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
    MediaFrame,
    ProtocolError,
    SeqCounter,
    UiState,
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

    def __init__(self, ws: web.WebSocketResponse, remote: str) -> None:
        self.ws = ws
        self.remote = remote
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

    async def send_interrupt(self) -> None:
        if self.link:
            self.link.send_msg(P.Interrupt())

    async def set_state(self, state: UiState) -> None:
        if self.link:
            self.link.send_msg(P.StateMsg(state=state))

    async def set_mic(self, enabled: bool) -> None:
        if self.link:
            self.link.send_msg(P.Mic(enabled=enabled, gain=self.cfg.mic_gain))

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
            await self._on_tap(msg)
        elif isinstance(msg, P.CameraStatusMsg):
            await self._on_camera_status(msg)
        elif isinstance(msg, P.AlarmStateMsg):
            self.alarms.on_state(msg)
            log.info(
                "alarm state: %d alarm(s), %d timer(s)%s%s",
                len(msg.alarms),
                len(msg.timers),
                "" if msg.exact_allowed else " [device cannot schedule exact alarms]",
                f" error={msg.error}" if msg.error else "",
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
        # Tap-to-talk is a permanent manual override, not a shim (HANDOFF 1).
        # It works even with wake word disabled or the model unreachable.
        log.info("tap-to-talk")
        if self.session.active:
            self.session.touch()
            return
        await self.set_state(UiState.LISTENING)
        await self.session.start("tap")

    async def _on_alarm_fired(self, msg: P.AlarmFired) -> None:
        """The device is already ringing; this is only so Gemini can say so.

        No attempt is made to open a session for it. Waking the model at 6 a.m.
        to narrate an alarm nobody asked it to narrate would be both expensive
        and rude; if a conversation happens to be open, it gets told.
        """
        log.info("device %s fired: %s", msg.kind.value, msg.label or "(no label)")
        await self.session.note_alarm_fired(msg.kind, msg.label)

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
        }

    async def shutdown(self) -> None:
        await self.weather.stop()
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

    link = DeviceLink(ws, request.remote or "?")
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
        ]
    )

    async def _on_startup(_: web.Application) -> None:
        # Started here rather than in Hub.__init__ because it needs a running
        # loop. Fire-and-forget: build_app must stay usable in tests that never
        # want a network fetch, so WEATHER_ENABLED=false simply skips it.
        if cfg.weather_enabled:
            hub.weather.start()

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

