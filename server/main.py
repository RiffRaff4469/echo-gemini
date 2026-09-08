"""echo-server: WebSocket hub + HTTP display API, one asyncio process.

The device dials out and holds exactly ONE persistent WebSocket (HANDOFF
section 3). Everything is multiplexed over it: mic audio up, camera frames up,
model audio down, display commands down. No inbound ports on the device, no NAT
problems, and reconnection logic lives in exactly one place -- the client.

Run it:

    python server/main.py

Or as a Windows at-boot service -- see ``server/install-service.ps1``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol as P  # noqa: E402
from alarms import AlarmCoordinator, summarise_state  # noqa: E402
from config import Config, ConfigError, load_config  # noqa: E402
from gemini_live import LiveSessionManager  # noqa: E402
from protocol import (  # noqa: E402
    Channel,
    DisplayCommand,
    MediaAction,
    MediaFrame,
    ProtocolError,
    SeqCounter,
    UiState,
)
from govee import GoveeHub
from memory import get_store
from spotify import (  # noqa: E402
    ALARM_HOLD,
    ALARM_HOLD_MAX_S,
    MUSIC_ERRORS,
    SpotifyController,
)
from wake import WakeWordEngine, build_detector  # noqa: E402
from weather import Reading, WeatherPoller  # noqa: E402

log = logging.getLogger("echo.server")

SECRET_HEADER = "X-Echo-Secret"
DISPLAY_SECRET_HEADER = "X-Shared-Secret"

# Bounded so a wedged device cannot make the server grow without limit. Audio
# down is the bulk of it: 24 kHz PCM16 is ~48 kB/s, so 400 chunks is seconds,
# not minutes, of backlog.
_SEND_QUEUE_MAX = 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeviceLink:
    """One live device WebSocket, with a single writer task.

    All sends funnel through a queue so the Live downlink, the heartbeat and
    ``POST /display`` can never interleave partial frames or block each other on
    a slow socket.
    """

    def __init__(
        self, ws: web.WebSocketResponse, remote: str, origin: str = ""
    ) -> None:
        self.ws = ws
        self.remote = remote
        # The base URL the DEVICE dialled, taken from the handshake's Host
        # header. The only address this server knows the device can resolve,
        # and therefore the only one worth putting in a card's image URL.
        self.origin = origin
        self.device_id = "?"
        self.app_version = "?"
        self.connected_at = time.time()
        self.last_pong = time.monotonic()
        self.dropped_sends = 0

        self._seq = SeqCounter()
        self._queue: asyncio.Queue[tuple[bool, Any] | None] = asyncio.Queue(
            _SEND_QUEUE_MAX
        )
        self._writer = asyncio.create_task(self._write_loop(), name="link-writer")
        self._closing = False

    # --- sending ----------------------------------------------------------

    def send_msg(self, msg: P.Message) -> None:
        self._enqueue(True, msg.encode())

    def send_frame(self, channel: Channel, payload: bytes) -> None:
        frame = MediaFrame(
            channel=channel, payload=payload, seq=self._seq.next(channel)
        )
        self._enqueue(False, frame.encode())

    def _enqueue(self, is_text: bool, data: Any) -> None:
        if self._closing or self.ws.closed:
            return
        try:
            self._queue.put_nowait((is_text, data))
        except asyncio.QueueFull:
            self.dropped_sends += 1
            if self.dropped_sends % 100 == 1:
                log.warning(
                    "device send queue full; dropped %d messages (slow link?)",
                    self.dropped_sends,
                )

    async def _write_loop(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                is_text, data = item
                if self.ws.closed:
                    return
                if is_text:
                    await self.ws.send_str(data)
                else:
                    await self.ws.send_bytes(data)
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, RuntimeError) as exc:
            log.info("device link write ended: %s", exc)
        except Exception:
            log.exception("device link writer failed")

    async def close(self, code: int = WSCloseCode.GOING_AWAY, reason: str = "") -> None:
        """Close cleanly, so a flapping device never leaves a half-open socket."""
        if self._closing:
            return
        self._closing = True
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        # Give the writer a moment to flush, then stop it regardless.
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(self._writer), timeout=2.0)
        self._writer.cancel()
        with contextlib.suppress(BaseException):
            await self._writer
        with contextlib.suppress(Exception):
            await self.ws.close(code=code, message=reason.encode()[:120])

    def info(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "app_version": self.app_version,
            "remote": self.remote,
            "connected_since": datetime.fromtimestamp(
                self.connected_at, timezone.utc
            ).isoformat(timespec="seconds"),
            "dropped_sends": self.dropped_sends,
        }


class Hub:
    """Routes everything between the device link, the wake word and Gemini.

    Implements ``gemini_live.SessionSink``: every method is a no-op when no
    device is attached, because the server outliving the device is normal and
    must never raise.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.link: DeviceLink | None = None
        self.started_at = time.time()
        # Layout focus (UI-BRIEF-14): chat while a session runs, music while
        # the display's speaker has a now-playing card, else home.
        self._last_focus: P.LayoutFocus | None = None
        self._music_card = False
        # Privacy mute mirror (v1.6): the DEVICE owns the real gate (audio
        # never leaves it); this flag mirrors it so the wake path here cannot
        # act on audio the device already stopped sending. Synced from the
        # device's hello caps (it persists mute across reboots).
        self.muted = False

        self._last_focus: P.LayoutFocus | None = None
        self._music_card = False

        self.wake = WakeWordEngine(
            build_detector(
                enabled=cfg.wake_enabled,
                model_name=cfg.wake_model,
                download=cfg.wake_download_models,
            ),
            threshold=cfg.wake_threshold,
            refractory_s=cfg.wake_refractory_s,
        )
        # Alarms live on the device; this only relays commands and acks
        # (see server/alarms.py for why the server holds no authoritative copy).
        self.alarms = AlarmCoordinator(
            self.push_alarm_command, timeout_s=cfg.alarm_ack_timeout_s
        )
        # None when SPOTIFY_ENABLED is off, and every caller treats that as
        # "this display has no music" rather than as an error -- the same shape
        # as Live with no API key.
        self.music: SpotifyController | None = (
            SpotifyController(
                cfg,
                on_pcm=self.push_music_audio,
                push_card=self.push_display,
                clear_card=self.clear_display,
                on_card_change=self._on_music_card,
            )
            if cfg.spotify_enabled
            else None
        )
        self.govee = GoveeHub(cfg.govee_devices) if cfg.govee_enabled else None
        self.memory = get_store()
        self.session = LiveSessionManager(cfg, self)
        # Decoration for the home screen, on its own background task. Nothing
        # in the voice path awaits it, and a failed fetch is logged and dropped
        # (see server/weather.py).
        self.weather = WeatherPoller(
            latitude=cfg.weather_lat,
            longitude=cfg.weather_lon,
            interval_s=cfg.weather_poll_s,
            on_reading=self.push_weather,
        )
        self._recorder: Any = None
        if cfg.record_audio_dir:
            self._recorder = _AudioRecorder(Path(cfg.record_audio_dir))

    async def remember(self, fact: str) -> dict[str, Any]:
        """Store a durable fact from the model (MEMORY-BRIEF-8)."""
        confirmation = self.memory.add_fact(fact, source="session")
        return {"confirmation": confirmation, "stored": fact}

    async def forget_memory(self, needle: str) -> dict[str, Any]:
        """Owner/admin cleanup: drop facts mentioning ``needle``."""
        return {"confirmation": self.memory.forget(needle)}

    async def govee_control(
        self, action: str, red: int | None = None, green: int | None = None,
        blue: int | None = None, percent: int | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Voice control; omitted target broadcasts to every configured strip."""
        if self.govee is None:
            return {"error": "Govee lights are not enabled."}
        try:
            if action in ("on", "off"):
                return await self.govee.set_power(action == "on", target)
            if action == "color":
                return await self.govee.set_color(red, green, blue, target)
            if action == "brightness":
                return await self.govee.set_brightness(percent, target)
            return {"error": f"Unknown Govee action: {action}"}
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}

    # --- link management --------------------------------------------------

    async def attach(self, link: DeviceLink) -> None:
        """Adopt a new link, evicting any previous one.

        A device that drops off Wi-Fi and reconnects will often produce a second
        connection before the server has noticed the first is dead. Replacing
        rather than rejecting is what keeps a flapping device usable.
        """
        old = self.link
        self.alarms.disconnected()
        self.link = link
        if self.music is not None:
            self.music.device_attached(link.origin)
        if old is not None:
            log.info("replacing existing device link from %s", old.remote)
            await old.close(WSCloseCode.SERVICE_RESTART, "replaced by new link")
        self.wake.reset()

    async def detach(self, link: DeviceLink) -> None:
        if self.link is not link:
            return  # already replaced; nothing to do
        self.link = None
        self.alarms.disconnected()
        log.info("device link from %s gone", link.remote)
        # No device means nothing to stream to. Close the billed session.
        await self.session.stop("device disconnected")
        self.wake.reset()

    @property
    def connected(self) -> bool:
        return self.link is not None and not self.link.ws.closed

    # --- SessionSink ------------------------------------------------------

    async def send_audio(self, pcm: bytes) -> None:
        if self.link:
            self.link.send_frame(Channel.AUDIO_DOWN, pcm)

    def push_music_audio(self, pcm: bytes) -> None:
        """Music, on its own channel. Synchronous: called from the event loop
        via ``call_soon_threadsafe`` off librespot's reader thread, where there
        is nothing to await into.

        A separate channel from ``send_audio`` on purpose -- see the note beside
        ``Channel.AUDIO_MUSIC``. Dropped silently with no device attached, which
        is the normal state for a server that outlives the Show.
        """
        if self.link:
            self.link.send_frame(Channel.AUDIO_MUSIC, pcm)

    async def send_interrupt(self) -> None:
        if self.link:
            self.link.send_msg(P.Interrupt())

    async def set_state(self, state: UiState) -> None:
        if self.link:
            self.link.send_msg(P.StateMsg(state=state))
        # A session starting or ending changes the layout focus (UI-BRIEF-14).
        self._maybe_push_layout()

    # --- layout focus (UI-BRIEF-14) -----------------------------------------

    def _on_music_card(self, active: bool) -> None:
        """The display's speaker card appeared or cleared -> re-evaluate."""
        self._music_card = active
        self._maybe_push_layout()

    def _current_focus(self) -> P.LayoutFocus:
        if self.session.active:
            return P.LayoutFocus.CHAT
        if self._music_card:
            return P.LayoutFocus.MUSIC
        return P.LayoutFocus.HOME

    def _maybe_push_layout(self) -> None:
        """Push ``layout`` only when the focus actually changed."""
        focus = self._current_focus()
        if focus == self._last_focus:
            return
        self._last_focus = focus
        if self.link:
            self.link.send_msg(P.LayoutMsg(focus=focus))

    async def set_mic(self, enabled: bool) -> None:
        if self.link:
            self.link.send_msg(P.Mic(enabled=enabled, gain=self.cfg.mic_gain))

    async def set_quiet(self, active: bool, closes_in_s: float) -> None:
        """Tell the device how long the post-answer window has left.

        Rounded because the device only uses it to arm a fade-in a few seconds
        before the close; sub-millisecond precision would just be noise on the
        wire and in the logs.
        """
        if self.link:
            self.link.send_msg(
                P.SessionQuiet(active=active, closes_in_s=round(closes_in_s, 2))
            )

    async def set_vision(self, enabled: bool) -> None:
        if not self.link:
            return
        log.info("vision %s", "requested" if enabled else "released")
        self.link.send_msg(
            P.Video(
                enabled=enabled,
                fps=self.cfg.vision_fps,
                jpeg_quality=self.cfg.vision_jpeg_quality,
            )
        )

    # --- inbound ----------------------------------------------------------

    async def on_text(self, link: DeviceLink, raw: str) -> None:
        if link is not self.link:
            return
        try:
            msg = P.decode(raw)
        except ProtocolError as exc:
            log.warning("bad control message from %s: %s", link.remote, exc)
            link.send_msg(P.ErrorMsg(code="bad_message", message=str(exc)))
            return

        if isinstance(msg, P.Hello):
            await self._on_hello(link, msg)
        elif isinstance(msg, P.Pong):
            link.last_pong = time.monotonic()
        elif isinstance(msg, P.Ping):
            link.send_msg(P.Pong(nonce=msg.nonce))
        elif isinstance(msg, P.Tap):
            # v1.6 (HARDWARE-BRIEF-7 v2): screen taps no longer start or stop
            # conversations -- the screen is for UI only. The wake word and the
            # physical mic button are the session triggers now. The message
            # stays decode-compatible for old clients but is a no-op.
            if msg.pressed:
                log.info("tap ignored: screen taps are UI-only since v1.6")
        elif isinstance(msg, (P.Stop, P.Stay)):
            # ``stay`` is v1.3's opposite intent, kept in the parser for a
            # device that has not been reflashed. Owner semantics supersede it:
            # a tap during a session ends the session either way.
            await self._on_stop()
        elif isinstance(msg, P.Button):
            await self._on_button(msg)
        elif isinstance(msg, P.MediaControl):
            await self._on_media_control(link, msg)
        elif isinstance(msg, P.CameraStatusMsg):
            await self._on_camera_status(msg)
        elif isinstance(msg, P.AlarmStateMsg):
            self.alarms.on_state(msg)
            await self._release_alarm_hold(msg)
            log.info(
                "alarm state: %d alarm(s), %d timer(s)%s%s",
                len(msg.alarms),
                len(msg.timers),
                "" if msg.exact_allowed else " [device cannot schedule exact alarms]",
                f" error={msg.error}" if msg.error else "",
            )
        elif isinstance(msg, P.StopwatchStateMsg):
            # v1.6 stopwatch mirror: the device owns the clock; this refreshes
            # the cache and answers any in-flight voice tool round trip.
            self.alarms.on_stopwatch_state(msg)
            log.info(
                "stopwatch state: %s at %d ms with %d lap(s)",
                "running" if msg.running else "stopped",
                msg.elapsed_ms,
                len(msg.laps_ms),
            )
        elif isinstance(msg, P.AlarmFired):
            await self._on_alarm_fired(msg)
        elif isinstance(msg, P.DeviceLog):
            log.log(
                _LEVELS.get(msg.level.lower(), logging.INFO),
                "[device] %s",
                msg.message,
            )
        elif isinstance(msg, P.ErrorMsg):
            log.error("[device] %s: %s", msg.code, msg.message)
        else:
            log.debug("ignoring %s from device", msg.TYPE.value)

    async def _on_hello(self, link: DeviceLink, msg: P.Hello) -> None:
        link.device_id = msg.device_id
        link.app_version = msg.app_version
        if msg.protocol_version != P.PROTOCOL_VERSION:
            log.error(
                "device %s speaks protocol v%d, server speaks v%d -- refusing",
                msg.device_id,
                msg.protocol_version,
                P.PROTOCOL_VERSION,
            )
            link.send_msg(
                P.ErrorMsg(
                    code="protocol_mismatch",
                    message=(
                        f"server speaks protocol v{P.PROTOCOL_VERSION}, "
                        f"device speaks v{msg.protocol_version}"
                    ),
                )
            )
            await link.close(WSCloseCode.POLICY_VIOLATION, "protocol mismatch")
            return

        if msg.protocol_minor != P.PROTOCOL_MINOR:
            # Additive skew only -- not fatal. A v1.0 device simply ignores
            # alarm_command and never sends alarm_state, so the tools time out
            # with a message instead of the server refusing to talk to it.
            log.warning(
                "device speaks protocol v1.%d, server speaks v1.%d; "
                "features added in the newer minor will not work",
                msg.protocol_minor,
                P.PROTOCOL_MINOR,
            )

        # Mute is device-owned and persists across reboots; a device that
        # boots muted announces it in caps so the server mirror starts right.
        self.muted = bool(msg.capabilities.get("muted", False))

        # Greet a fresh device with the current layout focus (UI-BRIEF-14).
        self._last_focus = None
        self._maybe_push_layout()

        log.info(
            "device %s (app %s, protocol v%d.%d) connected from %s; caps=%s",
            msg.device_id,
            msg.app_version,
            msg.protocol_version,
            msg.protocol_minor,
            link.remote,
            msg.capabilities or "{}",
        )
        link.send_msg(
            P.Welcome(
                heartbeat_interval_s=self.cfg.heartbeat_interval_s,
                live_enabled=self.cfg.live_enabled,
            )
        )
        link.send_msg(P.Mic(enabled=True, gain=self.cfg.mic_gain))
        link.send_msg(P.StateMsg(state=UiState.IDLE))
        # Ask for the schedule up front (no ack wait -- it is only a cache
        # refresh) so /health and the first list_alarms have something to say.
        link.send_msg(P.AlarmCommand(op=P.AlarmOp.LIST))
        # The home screen shows a placeholder until the first reading arrives,
        # so send whatever is cached now rather than making it wait out the
        # poll interval. With nothing cached yet, cut the current wait short.
        if self.cfg.weather_enabled:
            if self.weather.last is not None:
                self.push_weather(self.weather.last)
            else:
                self.weather.refresh_soon()

    async def _on_tap(self, msg: P.Tap) -> None:
        if not msg.pressed:
            return
        # Retired in v1.6 (HARDWARE-BRIEF-7 v2): screen taps are UI-only.
        log.info("tap ignored: screen taps are UI-only since v1.6")

    async def _on_button(self, msg: P.Button) -> None:
        """Physical mic button (v1.6). The device timed the press and sent the
        semantic action; the server only reacts to it."""
        if msg.action == "mute":
            # The device already flipped its local gate; mirror the flip here
            # so the server's wake path matches the device's deaf state.
            await self._apply_mute(not self.muted)
            return
        # talk_toggle
        if self.muted:
            # A short press unmutes AND talks (owner default (a)); the device
            # dropped its gate before sending this, so clear the mirror.
            log.info("mic button: unmuting to talk")
            self.muted = False
        if self.session.active:
            log.info("mic button: ending the conversation")
            await self._on_stop()
            return
        log.info("mic button: talk")
        await self.set_state(UiState.LISTENING)
        await self.session.start("button")

    async def _apply_mute(self, on: bool) -> None:
        """Server-side mirror of the device-owned privacy mute.

        The device applies the real gate (no audio leaves it). This mirror
        drops any audio that still arrives and is what ``set_mute`` toggles
        for the voice path ("Jarvis, go mute").
        """
        if self.muted == on:
            return
        self.muted = on
        log.info("privacy mute %s (server mirror)", "ON" if on else "off")

    async def set_mute(self, on: bool) -> dict[str, Any]:
        """Voice tool (``set_mute``): mute or unmute by request. Pushes the
        desired state to the device, which owns the gate + LED + chip."""
        await self._apply_mute(on)
        if self.connected and self.link is not None:
            self.link.send_msg(P.Mute(on=on))
        return {
            "confirmation": (
                "Muted -- the wake word is off and no audio leaves the "
                "display. Press and hold the mic button to unmute."
                if on
                else "Unmuted."
            )
        }

    async def _on_stop(self) -> None:
        """A tap while a session is already up: end it now (protocol v1.4).

        Separate from ``tap``, which OPENS a session, so the two intents cannot
        be confused -- and so an older client that only sends ``tap`` keeps
        behaving exactly as it did.

        Cutting the model off mid-answer is the point rather than a hazard, so
        this is ``stop`` and not a polite request to wind down. It is the same
        graceful close the quiet window performs, though: ``stop`` sets the
        session's stop event, the watchdog returns, and the session's own
        teardown releases the camera, reopens the mic and puts the display back
        to ambient. Nothing is killed, and the wake word re-arms by itself
        because mic audio goes back to the detector once the session is gone.

        A finger that lands in the same instant the session closes on its own
        arrives here with nothing to stop, and that is a no-op -- under the new
        semantics the user wanted it over, and it is over.
        """
        if not self.session.active:
            log.info("tap-to-stop with no session running; already back to ambient")
            return
        log.info("tap-to-stop -- ending the conversation")
        await self.session.stop("tap to stop")

    async def _on_alarm_fired(self, msg: P.AlarmFired) -> None:
        """The device is already ringing; this is only so Gemini can say so.

        No attempt is made to open a session for it. Waking the model at 6 a.m.
        to narrate an alarm nobody asked it to narrate would be both expensive
        and rude; if a conversation happens to be open, it gets told.

        It IS what stops the music, though. The alarm wins outright -- it is the
        one sound in the house with a deadline, and an alarm competing with an
        album is an alarm that gets slept through.
        """
        log.info("device %s fired: %s", msg.kind.value, msg.label or "(no label)")
        if self.music is not None:
            try:
                await self.music.hold(ALARM_HOLD, max_hold_s=ALARM_HOLD_MAX_S)
            except MUSIC_ERRORS:
                log.warning("could not pause the music for the alarm", exc_info=True)
        await self.session.note_alarm_fired(msg.kind, msg.label)

    async def _release_alarm_hold(self, state: P.AlarmStateMsg) -> None:
        """Give the speaker back once nothing on the device is ringing.

        The device pushes an unsolicited ``alarm_state`` on every change,
        including a Dismiss or a Snooze tapped on the panel
        (``AlarmScheduler.refreshRuntime``), so this is the dismissal signal --
        there is no separate message for it, and inventing one would mean a
        second thing that can disagree about whether an alarm is ringing.
        """
        if self.music is None:
            return
        if any(entry.ringing for entry in [*state.alarms, *state.timers]):
            return
        try:
            await self.music.release(ALARM_HOLD)
        except MUSIC_ERRORS:
            log.warning("could not resume the music after the alarm", exc_info=True)

    async def _on_media_control(self, link: DeviceLink, msg: P.MediaControl) -> None:
        """A transport button on the now-playing card (protocol v1.5).

        The device is deliberately not optimistic: it does not redraw the card
        as paused and hope. This call changes Spotify, the next ``now_playing``
        push reflects it, and the card is right because it was told, not because
        it guessed.
        """
        if self.music is None:
            link.send_msg(
                P.ErrorMsg(code="no_music", message="Spotify is not enabled")
            )
            return
        log.info("media control: %s", msg.action.value)
        action = msg.action
        if action is MediaAction.TOGGLE:
            action = MediaAction.PAUSE if self.music.is_playing else MediaAction.RESUME
        try:
            if action is MediaAction.PAUSE:
                await self.music.pause()
            elif action is MediaAction.RESUME:
                await self.music.resume()
            elif action is MediaAction.NEXT:
                await self.music.next_track()
            elif action is MediaAction.PREVIOUS:
                await self.music.previous_track()
        except MUSIC_ERRORS as exc:
            log.warning("media control %s failed: %s", action.value, exc)
            link.send_msg(P.ErrorMsg(code="media_failed", message=str(exc)))

    async def _on_camera_status(self, msg: P.CameraStatusMsg) -> None:
        level = (
            logging.WARNING
            if msg.status
            in (P.CameraStatus.ERROR, P.CameraStatus.SHUTTER_CLOSED)
            else logging.INFO
        )
        log.log(level, "camera: %s %s", msg.status.value, msg.detail)
        await self.session.note_camera_status(msg.status, msg.detail)

    async def on_binary(self, link: DeviceLink, raw: bytes) -> None:
        try:
            frame = MediaFrame.decode(raw)
        except ProtocolError as exc:
            log.warning("bad media frame from %s: %s", link.remote, exc)
            return

        if frame.channel is Channel.AUDIO_UP:
            await self._on_mic_audio(frame.payload)
        elif frame.channel is Channel.VIDEO_UP:
            self.session.feed_video(frame.payload)
        else:
            log.warning(
                "device sent on channel %s, which is server->device only",
                frame.channel.name,
            )

    async def _on_mic_audio(self, pcm: bytes) -> None:
        if self.muted:
            # Privacy mute mirror: the device's own gate should already have
            # stopped this stream; if anything still arrives, drop it here so
            # neither the detector nor a session can hear it.
            return
        if self._recorder is not None:
            self._recorder.write(pcm)

        if self.session.active:
            # Mid-conversation: audio belongs to the model, not the detector.
            # Scoring it here would let the model's own words re-trigger wake.
            self.session.feed_audio(pcm)
            return

        event = self.wake.feed(pcm)
        if event is None:
            return
        self.wake.reset()
        await self.set_state(UiState.LISTENING)
        await self.session.start(f"wake:{event.model}@{event.score:.2f}")

    # --- outbound API surface --------------------------------------------

    def push_display(self, cmd: DisplayCommand) -> None:
        if self.link:
            self.link.send_msg(P.Display(command=cmd))

    def push_alarm_command(self, cmd: P.AlarmCommand) -> bool:
        """Returns False when there is no device -- the coordinator turns that
        into "I could not reach the display" rather than a silent success."""
        if not self.connected or self.link is None:
            return False
        self.link.send_msg(cmd)
        return True

    def clear_display(self) -> None:
        if self.link:
            self.link.send_msg(P.DisplayClear())

    def push_weather(self, reading: Reading) -> None:
        """Send one observation down. A no-op with no device attached, which is
        the normal state for most of the ten minutes between polls."""
        if self.link:
            self.link.send_msg(
                P.WeatherMsg(
                    temp_c=reading.temp_c,
                    code=reading.code,
                    is_day=reading.is_day,
                )
            )

    def status(self) -> dict[str, Any]:
        # Null unless the model has finished answering and the session is
        # counting down to its own close -- see gemini_live._watchdog.
        closes_in = self.session.quiet_seconds_left if self.session.active else None
        return {
            "ok": True,
            "time": _now_iso(),
            "uptime_s": round(time.time() - self.started_at, 1),
            "device_connected": self.connected,
            "device": self.link.info() if self.link else None,
            "live_enabled": self.cfg.live_enabled,
            "live_model": self.cfg.gemini_model if self.cfg.live_enabled else None,
            "session_active": self.session.active,
            "session_idle_s": (
                round(self.session.idle_seconds, 1) if self.session.active else None
            ),
            "session_closes_in_s": (
                round(closes_in, 1) if closes_in is not None else None
            ),
            "vision_mode": self.cfg.vision_mode,
            "vision_streaming": self.session.vision_on,
            "wake_word": self.wake.detector.name if self.wake.active else None,
            "wake_last_score": round(self.wake.last_score, 3),
            # Last snapshot the device sent, not a server-side schedule -- if
            # this is null the device simply has not reported yet.
            "alarms": (
                summarise_state(self.alarms.last_state)
                if self.alarms.last_state is not None
                else None
            ),
            # Null until the first successful fetch -- the device renders a
            # placeholder for exactly this case.
            "weather": (
                {
                    "temp_c": self.weather.last.temp_c,
                    "code": self.weather.last.code,
                    "is_day": self.weather.last.is_day,
                    "age_s": round(self.weather.last.age_s(), 1),
                }
                if self.cfg.weather_enabled and self.weather.last is not None
                else None
            ),
            # Null when SPOTIFY_ENABLED is off. When it is on, this is the
            # first place to look for "why is there no music": whether the
            # account is linked, whether librespot is running, and how often
            # it has had to be restarted.
            "spotify": self.music.info() if self.music is not None else None,
        }

    async def shutdown(self) -> None:
        await self.weather.stop()
        if self.music is not None:
            await self.music.stop()
        await self.session.stop("server shutting down")
        if self.link:
            await self.link.close(WSCloseCode.GOING_AWAY, "server shutting down")
            self.link = None
        if self._recorder is not None:
            self._recorder.close()


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

CFG_KEY: web.AppKey[Config] = web.AppKey("cfg", Config)
HUB_KEY: web.AppKey[Hub] = web.AppKey("hub", Hub)


class _AudioRecorder:
    """Appends raw mic PCM to disk so the wake word can be retuned offline.

    HANDOFF section 7: iterating on logged audio is the whole reason wake word
    lives on the server. Raw headerless PCM16 @16 kHz -- import with
    ``ffmpeg -f s16le -ar 16000 -ac 1 -i <file> out.wav``.
    """

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"mic-{stamp}.pcm"
        self._fh = self.path.open("wb")
        log.info("recording mic audio to %s", self.path)

    def write(self, pcm: bytes) -> None:
        self._fh.write(pcm)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()


# ---------------------------------------------------------------------------
# HTTP / WebSocket routes
# ---------------------------------------------------------------------------


def _secret_ok(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


async def ws_handler(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app[HUB_KEY]
    cfg: Config = request.app[CFG_KEY]

    if not _secret_ok(request.headers.get(SECRET_HEADER), cfg.shared_secret):
        log.warning(
            "rejected WebSocket from %s: bad or missing %s",
            request.remote,
            SECRET_HEADER,
        )
        raise web.HTTPUnauthorized(text=f"{SECRET_HEADER} required\n")

    ws = web.WebSocketResponse(
        heartbeat=cfg.heartbeat_interval_s, max_msg_size=8 * 1024 * 1024
    )
    await ws.prepare(request)

    link = DeviceLink(
        ws,
        request.remote or "?",
        # aiohttp reports the HTTP scheme here, not the ws one, which is what
        # an <img src> needs anyway.
        origin=f"{request.scheme}://{request.host}" if request.host else "",
    )
    await hub.attach(link)
    heartbeat = asyncio.create_task(
        _heartbeat_loop(hub, link, cfg.heartbeat_interval_s), name="link-heartbeat"
    )
    try:
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                await hub.on_text(link, msg.data)
            elif msg.type is WSMsgType.BINARY:
                await hub.on_binary(link, msg.data)
            elif msg.type is WSMsgType.ERROR:
                log.warning("device socket error: %s", ws.exception())
                break
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("device socket handler failed")
    finally:
        heartbeat.cancel()
        with contextlib.suppress(BaseException):
            await heartbeat
        await link.close(WSCloseCode.GOING_AWAY, "closing")
        await hub.detach(link)
    return ws


async def _heartbeat_loop(hub: Hub, link: DeviceLink, interval: float) -> None:
    """Application-level ping/pong on top of the WS-level heartbeat.

    aiohttp's own ``heartbeat`` already kills half-open TCP. This adds an
    observable liveness signal at the app layer -- if the client's message loop
    is wedged but its socket is fine, only this notices.
    """
    nonce = 0
    deadline = interval * 3
    while not link.ws.closed:
        await asyncio.sleep(interval)
        if link.ws.closed:
            return
        silent = time.monotonic() - link.last_pong
        if silent > deadline:
            log.warning(
                "device %s missed pongs for %.0fs; dropping link",
                link.device_id,
                silent,
            )
            await link.close(WSCloseCode.GOING_AWAY, "heartbeat timeout")
            return
        nonce += 1
        link.send_msg(P.Ping(nonce=nonce))


def _display_authorized(request: web.Request) -> bool:
    cfg: Config = request.app[CFG_KEY]
    if not cfg.display_require_secret:
        return True
    provided = request.headers.get(DISPLAY_SECRET_HEADER) or request.headers.get(
        SECRET_HEADER
    )
    return _secret_ok(provided, cfg.shared_secret)


async def display_handler(request: web.Request) -> web.Response:
    hub: Hub = request.app[HUB_KEY]

    if not _display_authorized(request):
        return web.json_response(
            {"error": f"{DISPLAY_SECRET_HEADER} required"}, status=401
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be valid JSON"}, status=400)

    try:
        cmd = DisplayCommand.from_dict(body)
    except ProtocolError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not hub.connected:
        return web.json_response(
            {"error": "no device connected", "device_connected": False}, status=503
        )

    hub.push_display(cmd)
    log.info(
        "display push: type=%s duration=%.0fs priority=%d",
        cmd.type.value,
        cmd.duration,
        cmd.priority,
    )
    return web.json_response({"ok": True, "delivered_to": hub.link.device_id})


async def display_clear_handler(request: web.Request) -> web.Response:
    hub: Hub = request.app[HUB_KEY]
    if not _display_authorized(request):
        return web.json_response(
            {"error": f"{DISPLAY_SECRET_HEADER} required"}, status=401
        )
    if not hub.connected:
        return web.json_response({"error": "no device connected"}, status=503)
    hub.clear_display()
    return web.json_response({"ok": True})


async def vision_handler(request: web.Request) -> web.Response:
    """Manual vision override. The model normally opens the camera itself via
    the ``start_camera`` tool; this is the human escape hatch."""
    hub: Hub = request.app[HUB_KEY]
    if not _display_authorized(request):
        return web.json_response(
            {"error": f"{DISPLAY_SECRET_HEADER} required"}, status=401
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be valid JSON"}, status=400)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be true or false"}, status=400)
    if not hub.connected:
        return web.json_response({"error": "no device connected"}, status=503)
    if enabled and hub.cfg.vision_mode == "off":
        return web.json_response(
            {"error": "VISION_MODE=off; camera is disabled by configuration"},
            status=409,
        )
    if enabled and not hub.session.active:
        return web.json_response(
            {"error": "no active session; wake the device first"}, status=409
        )
    await hub.session.request_vision(enabled)
    return web.json_response({"ok": True, "vision_streaming": hub.session.vision_on})


async def spotify_art_handler(request: web.Request) -> web.Response:
    """Re-serve one track's album art from this machine.

    The device reaches this server over the tailnet and, by design, very little
    else -- the weather is polled here for exactly that reason. Handing the
    panel an ``i.scdn.co`` URL and hoping is how a now-playing card ends up with
    a hole in it, so the art comes from here by default.

    Unauthenticated, like ``/health``: it exposes an album cover for a track id
    the caller already had to know, and requiring the shared secret would mean
    putting it in an ``<img src>`` inside a WebView, which is worse than the
    thing it would be protecting.
    """
    hub: Hub = request.app[HUB_KEY]
    if hub.music is None:
        return web.json_response({"error": "spotify is not enabled"}, status=404)

    track_id = request.match_info.get("track_id", "")
    art = await hub.music.art.fetch(track_id)
    if art is None:
        return web.json_response({"error": "no art for that track"}, status=404)
    mime, body = art
    return web.Response(
        body=body,
        content_type=mime,
        # The art for a given track id never changes, and the card re-requests
        # it on every push; without this the device refetches an unchanging
        # 20 kB JPEG every few seconds.
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def health_handler(request: web.Request) -> web.Response:
    """Unauthenticated on purpose -- the service wrapper polls it, and it
    exposes no secrets."""
    hub: Hub = request.app[HUB_KEY]
    return web.json_response(hub.status())


def build_app(cfg: Config) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    hub = Hub(cfg)
    app[CFG_KEY] = cfg
    app[HUB_KEY] = hub
    app.add_routes(
        [
            web.get("/ws", ws_handler),
            web.get("/health", health_handler),
            web.post("/display", display_handler),
            web.post("/display/clear", display_clear_handler),
            web.post("/vision", vision_handler),
            web.get("/spotify/art/{track_id}", spotify_art_handler),
        ]
    )

    async def _on_startup(_: web.Application) -> None:
        # Started here rather than in Hub.__init__ because it needs a running
        # loop. Fire-and-forget: build_app must stay usable in tests that never
        # want a network fetch, so WEATHER_ENABLED=false simply skips it.
        if cfg.weather_enabled:
            hub.weather.start()
        if hub.music is not None:
            # Never fatal. An unlinked account or a missing librespot binary
            # logs how to fix it and leaves the rest of the server -- clock,
            # voice, alarms, display -- entirely unaffected.
            try:
                await hub.music.start()
            except Exception:
                log.exception("Spotify failed to start; continuing without it")

    async def _on_cleanup(_: web.Application) -> None:
        await hub.shutdown()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    # aiohttp logs every request at INFO; that is noise next to our own.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="echo-server for Echo Show 5")
    parser.add_argument("--env-file", help="path to a .env (default: repo root .env)")
    parser.add_argument("--host", help="override ECHO_HOST")
    parser.add_argument("--port", type=int, help="override ECHO_PORT")
    args = parser.parse_args(argv)

    _setup_logging(os.environ.get("LOG_LEVEL", "INFO").upper())

    try:
        cfg = load_config(args.env_file)
    except ConfigError as exc:
        print(f"\nconfiguration error:\n\n{exc}\n", file=sys.stderr)
        return 2

    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port

    _setup_logging(cfg.log_level)
    for warning in cfg.warnings:
        log.warning("%s", warning)
    log.info("echo-server starting: %s", cfg.describe())

    app = build_app(cfg)
    web.run_app(app, host=cfg.host, port=cfg.port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

