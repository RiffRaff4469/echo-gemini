"""Gemini Live session lifecycle.

One session at a time, opened on wake or tap and closed as soon as the
conversation is plainly over -- normally ``POST_ANSWER_SILENCE_S`` after the
model finishes answering, with ``SESSION_IDLE_TIMEOUT`` behind it as a backstop
(see ``_watchdog``). Closing promptly is cost control AND privacy, not polish:
Live sessions bill for the duration they are open (HANDOFF section 12), and for
as long as one is open the room's microphone is streaming to the model whether
or not anybody is talking to it.

Data paths, all pass-through -- no resampling anywhere:

    device mic  16 kHz PCM16 --> send_realtime_input(audio=...)
    model audio 24 kHz PCM16 <-- LiveServerMessage.data --> device speaker
    device cam  JPEG <=1 FPS  --> send_realtime_input(video=...)

The device produces exactly the rates the API wants (HANDOFF section 13), which
is why ``AudioCapture`` is pinned to 16 kHz and ``AudioPlayback`` to 24 kHz.

Written against ``google-genai`` 1.75: ``client.aio.live.connect(model=, config=)``
yields an ``AsyncSession`` with ``send_realtime_input`` / ``receive``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Protocol

from alarms import (
    AlarmCoordinator,
    AlarmError,
    normalise_days,
    resolve_alarm_time,
    resolve_duration,
    summarise_state,
)
from config import Config
from protocol import AUDIO_UP_RATE, VIDEO_MIME, AlarmKind, CameraStatus, UiState
from wake import EnergyVad

log = logging.getLogger("echo.live")

# The model asks to see rather than us guessing. VISION_MODE=on_demand exposes
# these two functions so the camera opens exactly when a session wants vision
# (HANDOFF section 7) and is released as soon as it does not.
LOOK_TOOL = "start_camera"
STOP_LOOK_TOOL = "stop_camera"

# Alarms and timers. Same pattern as the camera tools: the model calls, the
# handler talks to the device, and the tool response carries the state the
# device confirmed -- so what Gemini says out loud is what actually got set.
SET_ALARM_TOOL = "set_alarm"
SET_TIMER_TOOL = "set_timer"
CANCEL_ALARM_TOOL = "cancel_alarm"
CANCEL_TIMER_TOOL = "cancel_timer"
LIST_ALARMS_TOOL = "list_alarms"

_ALARM_TOOLS = frozenset(
    {
        SET_ALARM_TOOL,
        SET_TIMER_TOOL,
        CANCEL_ALARM_TOOL,
        CANCEL_TIMER_TOOL,
        LIST_ALARMS_TOOL,
    }
)

AUDIO_IN_MIME = f"audio/pcm;rate={AUDIO_UP_RATE}"

# Bound the uplink queue so a stalled API connection cannot grow unboundedly on
# a machine that is also the household's PC. Dropping the oldest audio is the
# right failure: stale mic audio is worthless.
_AUDIO_QUEUE_MAX = 200  # ~4 s of 20 ms frames
_VIDEO_QUEUE_MAX = 3

# One tap on the screen buys this much more conversation. Long enough to gather
# a thought and ask the follow-up, short enough that a stray elbow does not bill
# a minute of an empty room.
STAY_EXTENSION_S = 30.0

# Taps closer together than this are one tap. A finger on a panel that is also
# showing a fade-in hint produces several ACTION_DOWNs, and each of those must
# not silently buy another half minute.
STAY_DEBOUNCE_S = 0.75


class SessionSink(Protocol):
    """What a session needs to push back at the device. Implemented by ``Hub``."""

    alarms: AlarmCoordinator

    async def send_audio(self, pcm: bytes) -> None: ...
    async def send_interrupt(self) -> None: ...
    async def set_state(self, state: UiState) -> None: ...
    async def set_mic(self, enabled: bool) -> None: ...
    async def set_vision(self, enabled: bool) -> None: ...
    async def set_quiet(self, active: bool, closes_in_s: float) -> None: ...


class LiveUnavailable(RuntimeError):
    """Raised when a session is requested but no API key is configured."""


def _alarm_declarations() -> list[Any]:
    """Alarm and timer functions.

    Times are described to the model as a plain clock reading rather than an
    epoch, and resolved here against the server's clock. A model asked to do
    date arithmetic in its head will occasionally set an alarm for last
    Tuesday; ``alarms.resolve_alarm_time`` cannot.
    """
    from google.genai import types

    return [
        types.FunctionDeclaration(
            name=SET_ALARM_TOOL,
            description=(
                "Set an alarm on the display. Use this for a specific time of "
                "day ('wake me at 7', 'remind me at half past four'). The alarm "
                "rings on the device itself and keeps working even if this "
                "server is switched off. Normalize spoken clock times to HH:MM. "
                "Preserve 'today' or 'tomorrow' in the time argument when spoken. "
                "For other named dates resolve YYYY-MM-DD using the supplied local clock."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "time": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Time of day, 12- or 24-hour: '7', '7:05 am', "
                            "'19:30', 'tomorrow 7am', or 'today at 19:30'. If the user named an explicit calendar "
                            "date and time you may instead pass a full ISO-8601 "
                            "local datetime like '2026-09-07T07:05'."
                        ),
                    ),
                    "label": types.Schema(
                        type=types.Type.STRING,
                        description="Short name, e.g. 'Wake up'. Optional.",
                    ),
                    "days": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(type=types.Type.STRING),
                        description=(
                            "For a repeating alarm: day names such as "
                            "['mon','tue'], or 'weekdays' / 'weekends' / "
                            "'daily'. Omit for a one-off alarm."
                        ),
                    ),
                    "date": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Optional YYYY-MM-DD for a one-off alarm on a "
                            "specific day. Omit for the next occurrence."
                        ),
                    ),
                },
                required=["time"],
            ),
        ),
        types.FunctionDeclaration(
            name=SET_TIMER_TOOL,
            description=(
                "Start a countdown timer on the display ('ten minutes for the "
                "pasta'). It counts down and chimes on the device itself, with "
                "or without this server."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "duration_s": types.Schema(
                        type=types.Type.NUMBER,
                        description="Total length in seconds. Maximum 86400.",
                    ),
                    "label": types.Schema(
                        type=types.Type.STRING,
                        description="Short name, e.g. 'Pasta'. Optional.",
                    ),
                },
                required=["duration_s"],
            ),
        ),
        types.FunctionDeclaration(
            name=CANCEL_ALARM_TOOL,
            description=(
                "Cancel an alarm. Identify it by its label, or by the id from "
                "list_alarms. With neither, and exactly one alarm set, that one "
                "is cancelled; if several are set, ask which."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "label": types.Schema(type=types.Type.STRING),
                    "id": types.Schema(type=types.Type.STRING),
                },
            ),
        ),
        types.FunctionDeclaration(
            name=CANCEL_TIMER_TOOL,
            description=(
                "Cancel a running timer, by label or by the id from "
                "list_alarms. With neither, and exactly one timer running, that "
                "one is cancelled."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "label": types.Schema(type=types.Type.STRING),
                    "id": types.Schema(type=types.Type.STRING),
                },
            ),
        ),
        types.FunctionDeclaration(
            name=LIST_ALARMS_TOOL,
            description=(
                "Read back every alarm and running timer on the display. Call "
                "this before answering any question about what is set, rather "
                "than relying on what was said earlier in the conversation."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
    ]


def _build_tools(cfg: Config) -> list[Any]:
    """Everything the model may call: camera (``on_demand`` only) and alarms.

    In ``always`` vision mode frames stream for the whole session and the model
    has nothing to decide; in ``off`` mode the camera must never open, so
    declaring the function would be a lie. The alarm functions are always
    declared -- they need no hardware beyond the device already being there.
    """
    from google.genai import types

    declarations: list[Any] = list(_alarm_declarations())

    if cfg.vision_mode == "on_demand":
        declarations.extend(
            [
                types.FunctionDeclaration(
                    name=LOOK_TOOL,
                    description=(
                        "Open the device camera and begin receiving a live view of "
                        "the room at 1 frame per second. Call this when the user "
                        "asks about something they are showing you or about what "
                        "is physically in front of the device. A visible on-screen "
                        "indicator tells the user the camera is on."
                    ),
                    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
                ),
                types.FunctionDeclaration(
                    name=STOP_LOOK_TOOL,
                    description=(
                        "Close the device camera when you no longer need to see. "
                        "Call this as soon as the visual question is answered."
                    ),
                    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
                ),
            ]
        )

    return [types.Tool(function_declarations=declarations)]


def _system_instruction(cfg: Config) -> str:
    """The configured instruction plus the one fact a clock appliance needs.

    A Live model has no clock. Without being told the date and time it cannot
    reason about "tomorrow" at all, and -- worse for a device sitting on a
    kitchen counter -- it will confidently answer "what time is it?" with
    nothing. This is computed per session, so a session opened at 6 a.m. is not
    told yesterday's evening.
    """
    now = datetime.now().astimezone()
    return (
        f"{cfg.system_instruction}\n\n"
        f"[context] It is currently {now:%A %d %B %Y}, "
        f"{now:%H:%M} local time ({now.tzname() or 'local'}). "
        "You are attached to a display that keeps its own alarms and timers. "
        "Use the alarm functions to set, cancel or read them back rather than "
        "relying on memory -- the display rings on its own even when this "
        "server is off, and it is the only thing that knows what is set."
    )


def _build_connect_config(cfg: Config) -> Any:
    """Translate our ``Config`` into a ``LiveConnectConfig``.

    Imported lazily so the server starts, serves the display API and holds the
    device link even on a machine where ``google-genai`` is not importable.
    """
    from google.genai import types

    media_resolution = cfg.gemini_media_resolution.strip().upper()
    if not media_resolution.startswith("MEDIA_RESOLUTION_"):
        media_resolution = f"MEDIA_RESOLUTION_{media_resolution}"
    try:
        resolution = types.MediaResolution(media_resolution)
    except ValueError:
        log.warning(
            "GEMINI_MEDIA_RESOLUTION=%r is not a valid value; using MEDIUM",
            cfg.gemini_media_resolution,
        )
        resolution = types.MediaResolution.MEDIA_RESOLUTION_MEDIUM

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        media_resolution=resolution,
        tools=_build_tools(cfg),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=cfg.gemini_voice
                )
            )
        ),
        system_instruction=types.Content(
            parts=[types.Part(text=_system_instruction(cfg))]
        ),
        # Transcriptions are what make the server logs readable when tuning the
        # wake word against a weak microphone.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


class GenAiConnector:
    """Real connector. Wraps ``client.aio.live.connect`` as an async CM factory."""

    def __init__(self, cfg: Config) -> None:
        from google import genai
        from google.genai import types

        self._cfg = cfg
        self._client = genai.Client(
            api_key=cfg.gemini_api_key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )

    def connect(self):
        return self._client.aio.live.connect(
            model=self._cfg.gemini_model, config=_build_connect_config(self._cfg)
        )


class LiveSessionManager:
    """Owns at most one Live session and everything that feeds it.

    ``connector`` is injectable purely so tests can drive the whole lifecycle --
    open, audio round trip, barge-in, idle-close -- against a fake without a key
    or a network.
    """

    def __init__(
        self,
        cfg: Config,
        sink: SessionSink,
        *,
        connector: Any | None = None,
        clock=time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.sink = sink
        self._connector = connector
        self._clock = clock

        self._task: asyncio.Task | None = None
        self._session: Any = None
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue(_AUDIO_QUEUE_MAX)
        self._video_q: asyncio.Queue[bytes] = asyncio.Queue(_VIDEO_QUEUE_MAX)
        self._vad = EnergyVad(clock=clock)
        self._stop = asyncio.Event()
        self._started_at = 0.0
        self._last_activity = 0.0
        self._vision_on = False
        self.dropped_audio_frames = 0
        # Turn-end bookkeeping -- see _pump_end_turn.
        self._last_user_audio_at = 0.0
        self._user_turn_open = False
        self._model_speaking = False
        # Post-answer quiet window -- see _watchdog. ``_quiet_since`` is 0 when
        # no window is running, which is the case for all of a session that is
        # still mid-answer.
        self._quiet_since = 0.0
        self._stay_until = 0.0
        self._last_stay_at = 0.0
        self._announced_close_at = 0.0

    # --- state ------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def vision_on(self) -> bool:
        return self._vision_on

    def _connector_or_build(self) -> Any:
        if self._connector is None:
            if not self.cfg.live_enabled:
                raise LiveUnavailable(
                    "GEMINI_API_KEY is not set; cannot open a Live session"
                )
            self._connector = GenAiConnector(self.cfg)
        return self._connector

    # --- lifecycle --------------------------------------------------------

    async def start(self, reason: str) -> bool:
        """Open a session. Returns False if one is already running or Live is off.

        Never raises for the "no key" case -- that is a supported operating mode
        and the caller (a wake word or a tap) should not blow up because of it.
        """
        if self.active:
            # Re-arm the idle timer instead: the user speaking again mid-session
            # is the normal case, not an error.
            self.touch()
            return False
        if not self.cfg.live_enabled and self._connector is None:
            log.warning(
                "session requested (%s) but Gemini Live is disabled -- no GEMINI_API_KEY",
                reason,
            )
            await self.sink.set_state(UiState.IDLE)
            return False

        self._stop = asyncio.Event()
        self._audio_q = asyncio.Queue(_AUDIO_QUEUE_MAX)
        self._video_q = asyncio.Queue(_VIDEO_QUEUE_MAX)
        self._vad.reset()
        self.dropped_audio_frames = 0
        self._last_user_audio_at = 0.0
        self._user_turn_open = False
        self._model_speaking = False
        self._quiet_since = 0.0
        self._stay_until = 0.0
        self._last_stay_at = 0.0
        self._announced_close_at = 0.0
        self._started_at = self._clock()
        self.touch()
        log.info("opening Live session (%s) model=%s", reason, self.cfg.gemini_model)
        self._task = asyncio.create_task(self._run(), name="live-session")
        return True

    async def stop(self, reason: str) -> None:
        if self._task is None:
            return
        log.info("closing Live session (%s)", reason)
        self._stop.set()
        # Unblock the uplink pump if it is waiting on an empty queue.
        try:
            self._audio_q.put_nowait(None)
        except asyncio.QueueFull:
            pass
        task, self._task = self._task, None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("Live session did not close in 10 s; cancelling")
            task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Live session raised while closing")

    def touch(self) -> None:
        """Mark the session as in use -- resets the idle-close timer.

        Deliberately does NOT poke the VAD: the VAD's hangover would then treat
        the following second of silence as speech and quietly extend a billed
        session that nobody is using.
        """
        self._last_activity = self._clock()

    @property
    def idle_seconds(self) -> float:
        return self._clock() - self._last_activity

    # --- the post-answer quiet window -------------------------------------

    @property
    def quiet_seconds_left(self) -> float | None:
        """Seconds until the post-answer close, or None when no window is open.

        None is the answer for most of a session: the window exists only
        between the model finishing an answer and someone continuing.
        """
        if not self._quiet_since:
            return None
        return max(0.0, self._closes_at() - self._clock())

    def _closes_at(self) -> float:
        """When the running quiet window will end the session.

        A tap pushes ``_stay_until`` out past the plain window, so the later of
        the two is the real deadline -- one tap during a five-second window must
        not be undone by the window's own arithmetic.
        """
        return max(
            self._quiet_since + self.cfg.post_answer_silence_s, self._stay_until
        )

    def _open_quiet_window(self) -> None:
        """The model just finished answering: start counting down to the close.

        Only ever called from ``_pump_down`` on ``turn_complete``, which is the
        one point where the model is definitively not speaking. Everything that
        counts as the conversation continuing -- more model audio, user speech,
        a barge-in -- clears it again, so a window can never be running while an
        answer is in flight.
        """
        if self.cfg.post_answer_silence_s <= 0:
            return  # disabled; SESSION_IDLE_TIMEOUT remains the only close
        if not self._quiet_since:
            self._quiet_since = self._clock()

    def _cancel_quiet_window(self) -> None:
        """The conversation continued. Forget the deadline entirely.

        ``_stay_until`` survives on purpose: a tap made during an answer should
        still be honoured by the window that opens when the answer ends.
        """
        self._quiet_since = 0.0

    def extend_stay(self) -> bool:
        """Tap-to-stay: hold this session open for another ``STAY_EXTENSION_S``.

        Returns False for a bounced duplicate tap, so the caller can log one
        extension rather than four. Also re-arms the idle timer -- a tap is a
        person at the display, which is exactly what ``SESSION_IDLE_TIMEOUT``
        is trying to detect the absence of.
        """
        now = self._clock()
        if now - self._last_stay_at < STAY_DEBOUNCE_S:
            return False
        self._last_stay_at = now
        self._stay_until = max(self._stay_until, now) + STAY_EXTENSION_S
        self.touch()
        return True

    async def _publish_quiet(self, now: float) -> None:
        """Keep the device's copy of the deadline in step with ours.

        Sent only when the deadline actually moves, so a window that simply
        counts down costs one message at each end of it rather than one per
        watchdog tick.
        """
        target = self._closes_at() if self._quiet_since else 0.0
        if target == self._announced_close_at:
            return
        self._announced_close_at = target
        try:
            if target:
                await self.sink.set_quiet(True, max(0.0, target - now))
            else:
                await self.sink.set_quiet(False, 0.0)
        except Exception:
            # A hint the device never receives is a missing hint, not a reason
            # to take the conversation down.
            log.exception("failed to push the quiet-window hint")

    # --- ingress ----------------------------------------------------------

    def feed_audio(self, pcm: bytes) -> None:
        """Queue device mic audio. Non-blocking; drops oldest under pressure."""
        if not self.active or not pcm:
            return
        if self._vad.feed(pcm):
            self._last_activity = self._clock()
            # Speaking again inside the post-answer window is a follow-up, not
            # a race against the close: the window goes away and the session
            # carries on turn by turn.
            self._cancel_quiet_window()
        try:
            self._audio_q.put_nowait(pcm)
        except asyncio.QueueFull:
            self.dropped_audio_frames += 1
            try:
                self._audio_q.get_nowait()  # drop oldest, keep the freshest audio
                self._audio_q.put_nowait(pcm)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            if self.dropped_audio_frames % 50 == 1:
                log.warning(
                    "uplink audio queue full; dropped %d frames total",
                    self.dropped_audio_frames,
                )

    def feed_video(self, jpeg: bytes) -> None:
        """Queue a camera frame. At <=1 FPS this queue should never be deep; if
        it is, the newest frame is what matters, so the oldest is dropped."""
        if not self.active or not jpeg:
            return
        self._last_activity = self._clock()
        try:
            self._video_q.put_nowait(jpeg)
        except asyncio.QueueFull:
            try:
                self._video_q.get_nowait()
                self._video_q.put_nowait(jpeg)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def request_vision(self, enabled: bool) -> None:
        """Turn the device camera on or off for this session.

        The camera opens only here and is released the moment the session ends
        (HANDOFF section 8.3).
        """
        if enabled == self._vision_on:
            return
        self._vision_on = enabled
        await self.sink.set_vision(enabled)

    # --- the session itself -----------------------------------------------

    async def _run(self) -> None:
        pumps: list[asyncio.Task] = []
        try:
            connector = self._connector_or_build()
            async with connector.connect() as session:
                self._session = session
                await self.sink.set_state(UiState.LISTENING)
                if self.cfg.vision_mode == "always":
                    await self.request_vision(True)

                pumps = [
                    asyncio.create_task(self._pump_audio_up(session), name="live-audio-up"),
                    asyncio.create_task(self._pump_video_up(session), name="live-video-up"),
                    asyncio.create_task(self._pump_down(session), name="live-down"),
                    asyncio.create_task(self._pump_end_turn(session), name="live-end-turn"),
                    asyncio.create_task(self._watchdog(), name="live-watchdog"),
                ]
                done, pending = await asyncio.wait(
                    pumps, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        log.error("Live pump %s failed: %r", task.get_name(), exc)
        except LiveUnavailable as exc:
            log.warning("%s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Live session error; will reopen on next wake")
        finally:
            for task in pumps:
                task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            self._session = None
            self._stop.set()
            # Release the camera unconditionally. A session that ended for ANY
            # reason -- idle, error, device drop -- must not leave the HAL1
            # camera open (HANDOFF section 8.3).
            try:
                if self._vision_on:
                    self._vision_on = False
                    await self.sink.set_vision(False)
                # The window is over one way or another; a hint left fading in
                # on a device whose session has already gone is a lie.
                self._cancel_quiet_window()
                await self._publish_quiet(self._clock())
                await self.sink.set_mic(True)
                await self.sink.set_state(UiState.IDLE)
            except Exception:
                log.exception("failed to reset device state after session close")
            log.info(
                "Live session ended after %.1fs (dropped %d audio frames)",
                self._clock() - self._started_at,
                self.dropped_audio_frames,
            )

    async def _pump_audio_up(self, session: Any) -> None:
        from google.genai import types

        while not self._stop.is_set():
            pcm = await self._audio_q.get()
            if pcm is None or self._stop.is_set():
                return
            await session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=AUDIO_IN_MIME)
            )
            # The user is mid-utterance; _pump_end_turn closes the turn once
            # these stop arriving.
            self._last_user_audio_at = self._clock()
            self._user_turn_open = True

    async def _pump_end_turn(self, session: Any) -> None:
        """Close the user's turn explicitly after trailing silence.

        The Live API does NOT end a user turn by itself when audio arrives via
        ``send_realtime_input``. Verified against the live API on 2026-09-03: a
        session fed a complete 5 s utterance sat silent for 30+ s and produced
        zero model audio. Without this the wake-word path -- the single most
        important path in the app -- never gets a reply.

        The signal that works is ``audio_stream_end``, checked the same day
        against ``gemini-3.1-flash-live-preview``: streaming the 5 s sample and
        then sending it produced a transcript and 43 audio chunks, where doing
        nothing produced 0 and ``send_client_content(turn_complete=True)`` also
        produced 0. Manual activity detection (``activity_start``/``_end`` with
        ``automatic_activity_detection`` disabled) works too, but it turns off
        the server-side VAD that raises the ``interrupted`` messages barge-in
        depends on, so it would cost more than it buys. Sending audio again
        after ``audio_stream_end`` simply reopens the stream.

        The device VAD-gates silence before it ever reaches us, so "no frames
        for ``SESSION_END_TURN_SILENCE_S``" *is* the end of the utterance;
        there is nothing further to detect. Two guards keep this from firing
        wrongly: never while the model is speaking (interrupting it is
        barge-in, which the API signals to us instead), and never when the user
        has not actually said anything since the last model turn.
        """
        window = self.cfg.session_end_turn_silence_s
        if window <= 0:
            await self._stop.wait()  # disabled, but must not end the session
            return
        tick = max(0.05, window / 4)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick)
                return
            except asyncio.TimeoutError:
                pass
            if not self._user_turn_open or self._model_speaking:
                continue
            if self._clock() - self._last_user_audio_at < window:
                continue
            self._user_turn_open = False
            log.info("user silent for %.1fs -- ending the user turn", window)
            try:
                await session.send_realtime_input(audio_stream_end=True)
            except Exception:
                log.exception("failed to send turn-end")

    async def _pump_video_up(self, session: Any) -> None:
        from google.genai import types

        # Hard-enforce the API's 1 FPS ceiling here as well as on the device;
        # extra frames are billed work that the API discards anyway.
        min_interval = 1.0 / max(self.cfg.vision_fps, 0.01)
        last_sent = float("-inf")
        while not self._stop.is_set():
            jpeg = await self._video_q.get()
            now = self._clock()
            if now - last_sent < min_interval:
                continue
            last_sent = now
            await session.send_realtime_input(
                video=types.Blob(data=jpeg, mime_type=VIDEO_MIME)
            )

    async def _pump_down(self, session: Any) -> None:
        """Model output: audio to the speaker, interrupts to the flush path.

        ``session.receive()`` yields ONE model turn and then ends (google-genai
        1.75 breaks its loop on ``turn_complete``), so it has to be re-entered
        per turn. Without the outer loop this pump would finish after the
        model's first reply and take the whole session down with it, leaving
        the user unable to ask a follow-up without saying the wake word again.
        """
        while not self._stop.is_set():
            received = False
            async for message in session.receive():
                received = True
                if self._stop.is_set():
                    return

                content = getattr(message, "server_content", None)

                if content is not None and getattr(content, "interrupted", False):
                    # Barge-in: the API tells us the user spoke over the model.
                    # Everything already queued in AudioTrack is now wrong.
                    log.info("barge-in: flushing device playback")
                    self._model_speaking = False
                    self._last_activity = self._clock()
                    self._cancel_quiet_window()
                    await self.sink.send_interrupt()
                    await self.sink.set_mic(True)
                    await self.sink.set_state(UiState.LISTENING)
                    continue

                audio = getattr(message, "data", None)
                if audio:
                    # The model answered, so there is no open user turn left to
                    # close -- and no turn-end may be sent over its voice.
                    self._user_turn_open = False
                    # Model output is activity in its own right. Counting it is
                    # what lets a long answer run past POST_ANSWER_SILENCE_S --
                    # and past SESSION_IDLE_TIMEOUT -- without being cut off.
                    self._cancel_quiet_window()
                    if not self._model_speaking:
                        self._model_speaking = True
                        # Half-duplex: close the mic while we talk rather than
                        # doing AEC on one weak microphone (HANDOFF 8.2).
                        await self.sink.set_mic(False)
                        await self.sink.set_state(UiState.SPEAKING)
                    self._last_activity = self._clock()
                    await self.sink.send_audio(audio)

                if content is not None:
                    self._log_transcripts(content)
                    if getattr(content, "turn_complete", False):
                        self._model_speaking = False
                        self._user_turn_open = False
                        self._last_activity = self._clock()
                        # The answer is finished. From here the session has
                        # POST_ANSWER_SILENCE_S to be given a reason to stay
                        # open; otherwise the watchdog closes it and the mic
                        # stops being streamed anywhere.
                        self._open_quiet_window()
                        await self.sink.set_mic(True)
                        await self.sink.set_state(UiState.LISTENING)

                tool_call = getattr(message, "tool_call", None)
                if tool_call is not None and tool_call.function_calls:
                    await self._handle_tool_calls(session, tool_call.function_calls)

                go_away = getattr(message, "go_away", None)
                if go_away is not None:
                    log.info(
                        "server sent GoAway (time_left=%s); closing", go_away.time_left
                    )
                    return

            if not received:
                # A pass that yields nothing means the socket is finished, not
                # that a turn ended. Re-entering would spin.
                return

    async def _handle_tool_calls(self, session: Any, calls: list[Any]) -> None:
        from google.genai import types

        # A tool call is the model still working on the answer, and a slow one
        # (an alarm waiting on the device to ack) can outlast the quiet window.
        self._last_activity = self._clock()
        self._cancel_quiet_window()
        responses = []
        for call in calls:
            result = await self._run_tool(call)
            responses.append(
                types.FunctionResponse(id=call.id, name=call.name, response=result)
            )
        if responses:
            await session.send_tool_response(function_responses=responses)

    async def _run_tool(self, call: Any) -> dict[str, Any]:
        """Run one function call. Never raises -- a tool that blows up must come
        back as an error the model can say out loud, not as a dead session."""
        if call.name == LOOK_TOOL:
            await self.request_vision(True)
            return {
                "status": "camera_opening",
                "detail": (
                    "Frames will arrive at 1 FPS shortly. The device is showing "
                    "the user a camera-active indicator."
                ),
            }
        if call.name == STOP_LOOK_TOOL:
            await self.request_vision(False)
            return {"status": "camera_closed"}
        if call.name in _ALARM_TOOLS:
            return await self._run_alarm_tool(call.name, dict(call.args or {}))

        log.warning("model called unknown tool %r", call.name)
        return {"error": f"unknown function {call.name}"}

    async def _run_alarm_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Apply an alarm tool call on the device and answer with what it did.

        The response is always the device's confirmed state, never the request:
        the model saying "done, alarm set for 7:05" has to be backed by the
        device having said so, or the user gets a promise nothing kept.
        """
        coordinator: AlarmCoordinator | None = getattr(self.sink, "alarms", None)
        if coordinator is None:
            return {"error": "This display does not support alarms."}

        # Tool calls are user intent, so they keep the billed session alive the
        # same way speech does -- otherwise a long "set an alarm... no, make it
        # 7:15" exchange can idle out mid-correction.
        self.touch()

        try:
            state = await self._dispatch_alarm(coordinator, name, args)
        except AlarmError as exc:
            # Includes bad times, offline device and ack timeouts. All of these
            # are things the user should simply be told.
            log.warning("alarm tool %s failed: %s", name, exc)
            return {"error": str(exc)}
        except Exception:
            log.exception("alarm tool %s crashed", name)
            return {"error": "Something went wrong talking to the display."}

        summary = summarise_state(state)
        log.info("alarm tool %s -> %s", name, summary["summary"])
        return summary

    @staticmethod
    async def _dispatch_alarm(
        coordinator: AlarmCoordinator, name: str, args: dict[str, Any]
    ) -> Any:
        if name == SET_ALARM_TOOL:
            days = normalise_days(args.get("days"))
            epoch_ms = resolve_alarm_time(
                str(args.get("time") or ""),
                date=str(args.get("date") or ""),
                days=days,
            )
            return await coordinator.set_alarm(
                label=str(args.get("label") or ""),
                time_epoch_ms=epoch_ms,
                days=days,
            )

        if name == SET_TIMER_TOOL:
            # Only duration_s is declared, but models volunteer `minutes` often
            # enough that silently ignoring it would produce a timer of zero.
            duration_s = resolve_duration(
                args.get("duration_s"),
                hours=float(args.get("hours") or 0),
                minutes=float(args.get("minutes") or 0),
                seconds=float(args.get("seconds") or 0),
            )
            return await coordinator.set_timer(
                label=str(args.get("label") or ""), duration_s=duration_s
            )

        if name in (CANCEL_ALARM_TOOL, CANCEL_TIMER_TOOL):
            kind = (
                AlarmKind.ALARM.value
                if name == CANCEL_ALARM_TOOL
                else AlarmKind.TIMER.value
            )
            return await coordinator.cancel(
                entry_id=str(args.get("id") or ""),
                label=str(args.get("label") or ""),
                kind=kind,
            )

        return await coordinator.list_all()

    async def note_alarm_fired(self, kind: AlarmKind, label: str) -> None:
        """Tell an open session that the device just started ringing.

        Injected as a text note the same way camera status is. It is not what
        makes the noise -- the device is already chiming locally -- it just lets
        a conversation in progress acknowledge it instead of talking over a
        beeping panel.
        """
        session = self._session
        if session is None or not self.active:
            return
        what = f"{kind.value} “{label}”" if label else f"{kind.value}"
        text = (
            f"[device] The {what} just went off and the display is chiming. "
            "Briefly tell the user, then stop. They can tap Dismiss on the screen. "
            + ("Alarms also have Snooze for five minutes." if kind is AlarmKind.ALARM else "")
        )
        log.info("relaying %s fired to the model", kind.value)
        self.touch()
        # The model is about to say something unprompted; do not close out from
        # under it because the quiet window happened to be counting down.
        self._cancel_quiet_window()
        try:
            await session.send_realtime_input(text=text)
        except Exception:
            log.exception("failed to relay the alarm to the model")

    async def note_camera_status(self, status: CameraStatus, detail: str) -> None:
        """Relay a device camera event into the conversation as text.

        The physical privacy shutter is the point of this: patches 0015/0016
        keep the camera enumerated with the latch engaged, so frames come back
        black rather than erroring. Telling the model in words beats silently
        streaming black JPEGs to it (HANDOFF section 8.3).
        """
        session = self._session
        if session is None or not self.active:
            return
        if status is CameraStatus.SHUTTER_CLOSED:
            text = (
                "[device] The camera's physical privacy shutter is closed, so there "
                "is no view available. Tell the user to slide the shutter open if "
                "they want you to see."
            )
        elif status is CameraStatus.ERROR:
            text = (
                f"[device] The camera could not be opened ({detail or 'unknown error'}). "
                "You cannot see right now; continue without vision."
            )
        else:
            return
        log.info("relaying camera status to model: %s", status.value)
        try:
            await session.send_realtime_input(text=text)
        except Exception:
            log.exception("failed to relay camera status")

    @staticmethod
    def _log_transcripts(content: Any) -> None:
        heard = getattr(content, "input_transcription", None)
        if heard is not None and getattr(heard, "text", None):
            log.info("heard: %s", heard.text)
        said = getattr(content, "output_transcription", None)
        if said is not None and getattr(said, "text", None):
            log.debug("said: %s", said.text)

    async def _watchdog(self) -> None:
        """Close the session when the conversation is over, or when it overruns.

        Three closes, in the order they normally fire:

        * **The post-answer quiet window** (``POST_ANSWER_SILENCE_S``, ~8 s) is
          the one that matters day to day. It starts only when the model has
          finished answering and ends the session unless the user speaks again
          or taps the screen. A quick question therefore costs about ten
          seconds of billed session, and -- just as importantly -- the mic
          stops being streamed to the model straight afterwards, so the
          conversation someone has next to the display is not sent anywhere.
        * **``SESSION_IDLE_TIMEOUT``** (120 s) remains the backstop for a
          session that never gets an answer out of the model at all, and the
          only close when the quiet window is disabled.
        * **``SESSION_MAX_DURATION``** is the hard ceiling. It is the sole rule
          here that can fire mid-answer, deliberately: it is the last defence
          against a session that never ends, and at ten minutes an answer still
          running into it has already gone wrong.

        Nothing else can cut an answer short. While ``_model_speaking`` is true
        the idle clock is held open and no quiet window can exist, so a long
        reply always completes.
        """
        idle_limit = self.cfg.session_idle_timeout_s
        max_duration = self.cfg.session_max_duration_s
        quiet_limit = self.cfg.post_answer_silence_s
        # Check often enough that whichever limit is shortest is honoured
        # promptly, but not so often that an idle server spins. The quiet
        # window is in the list because the device's "tap to keep talking" hint
        # is armed off this tick, and a late hint is a hint nobody can act on.
        limits = [x for x in (idle_limit, max_duration, quiet_limit) if x > 0]
        tick = min(1.0, max(0.05, min(limits) / 4)) if limits else 1.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick)
                return
            except asyncio.TimeoutError:
                pass

            now = self._clock()
            if self._model_speaking:
                # An answer in flight. Hold both soft clocks open rather than
                # merely ignoring them, so a reply longer than the idle timeout
                # does not close the instant it finishes.
                self._last_activity = now
                self._cancel_quiet_window()

            await self._publish_quiet(now)

            if max_duration > 0 and (now - self._started_at) >= max_duration:
                log.info("session hit SESSION_MAX_DURATION (%.0fs) -- closing", max_duration)
                return
            if self._model_speaking:
                continue
            if self._quiet_since and now >= self._closes_at():
                log.info(
                    "quiet for %.1fs after the answer -- closing back to ambient",
                    now - self._quiet_since,
                )
                return
            idle = self.idle_seconds
            if idle_limit > 0 and idle >= idle_limit:
                log.info("idle for %.0fs -- closing billed session", idle)
                return
