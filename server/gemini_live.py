"""Gemini Live session lifecycle.

One session at a time, opened on wake or tap and closed on idle. Idle-close is
cost control, not polish -- Live sessions bill for the duration they are open
(HANDOFF section 12), and a device that streams gated audio into a session
nobody is using would bill all day.

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
from typing import Any, Protocol

from config import Config
from protocol import AUDIO_UP_RATE, VIDEO_MIME, CameraStatus, UiState
from wake import EnergyVad

log = logging.getLogger("echo.live")

# The model asks to see rather than us guessing. VISION_MODE=on_demand exposes
# these two functions so the camera opens exactly when a session wants vision
# (HANDOFF section 7) and is released as soon as it does not.
LOOK_TOOL = "start_camera"
STOP_LOOK_TOOL = "stop_camera"

AUDIO_IN_MIME = f"audio/pcm;rate={AUDIO_UP_RATE}"

# Bound the uplink queue so a stalled API connection cannot grow unboundedly on
# a machine that is also the household's PC. Dropping the oldest audio is the
# right failure: stale mic audio is worthless.
_AUDIO_QUEUE_MAX = 200  # ~4 s of 20 ms frames
_VIDEO_QUEUE_MAX = 3


class SessionSink(Protocol):
    """What a session needs to push back at the device. Implemented by ``Hub``."""

    async def send_audio(self, pcm: bytes) -> None: ...
    async def send_interrupt(self) -> None: ...
    async def set_state(self, state: UiState) -> None: ...
    async def set_mic(self, enabled: bool) -> None: ...
    async def set_vision(self, enabled: bool) -> None: ...


class LiveUnavailable(RuntimeError):
    """Raised when a session is requested but no API key is configured."""


def _build_tools(cfg: Config) -> list[Any]:
    """Camera control exposed to the model, only in ``on_demand`` mode.

    In ``always`` mode frames stream for the whole session and the model has
    nothing to decide; in ``off`` mode the camera must never open, so declaring
    the function would be a lie.
    """
    if cfg.vision_mode != "on_demand":
        return []
    from google.genai import types

    return [
        types.Tool(
            function_declarations=[
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
    ]


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
            parts=[types.Part(text=cfg.system_instruction)]
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

    # --- ingress ----------------------------------------------------------

    def feed_audio(self, pcm: bytes) -> None:
        """Queue device mic audio. Non-blocking; drops oldest under pressure."""
        if not self.active or not pcm:
            return
        if self._vad.feed(pcm):
            self._last_activity = self._clock()
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
        """Model output: audio to the speaker, interrupts to the flush path."""
        speaking = False
        async for message in session.receive():
            if self._stop.is_set():
                return

            content = getattr(message, "server_content", None)

            if content is not None and getattr(content, "interrupted", False):
                # Barge-in: the API tells us the user spoke over the model.
                # Everything already queued in AudioTrack is now wrong.
                log.info("barge-in: flushing device playback")
                speaking = False
                self._last_activity = self._clock()
                await self.sink.send_interrupt()
                await self.sink.set_mic(True)
                await self.sink.set_state(UiState.LISTENING)
                continue

            audio = getattr(message, "data", None)
            if audio:
                if not speaking:
                    speaking = True
                    # Half-duplex: close the mic while we talk rather than doing
                    # AEC on one weak microphone (HANDOFF section 8.2).
                    await self.sink.set_mic(False)
                    await self.sink.set_state(UiState.SPEAKING)
                self._last_activity = self._clock()
                await self.sink.send_audio(audio)

            if content is not None:
                self._log_transcripts(content)
                if getattr(content, "turn_complete", False):
                    speaking = False
                    self._last_activity = self._clock()
                    await self.sink.set_mic(True)
                    await self.sink.set_state(UiState.LISTENING)

            tool_call = getattr(message, "tool_call", None)
            if tool_call is not None and tool_call.function_calls:
                await self._handle_tool_calls(session, tool_call.function_calls)

            go_away = getattr(message, "go_away", None)
            if go_away is not None:
                log.info("server sent GoAway (time_left=%s); closing", go_away.time_left)
                return

    async def _handle_tool_calls(self, session: Any, calls: list[Any]) -> None:
        from google.genai import types

        responses = []
        for call in calls:
            if call.name == LOOK_TOOL:
                await self.request_vision(True)
                result = {
                    "status": "camera_opening",
                    "detail": (
                        "Frames will arrive at 1 FPS shortly. The device is showing "
                        "the user a camera-active indicator."
                    ),
                }
            elif call.name == STOP_LOOK_TOOL:
                await self.request_vision(False)
                result = {"status": "camera_closed"}
            else:
                log.warning("model called unknown tool %r", call.name)
                result = {"error": f"unknown function {call.name}"}
            responses.append(
                types.FunctionResponse(id=call.id, name=call.name, response=result)
            )
        if responses:
            await session.send_tool_response(function_responses=responses)

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
        """Close the session when it goes quiet, or when it runs too long.

        This is the billing control. ``SESSION_IDLE_TIMEOUT`` (default 120 s) of
        no speech in either direction ends the session; the next wake word or
        tap opens a fresh one.
        """
        idle_limit = self.cfg.session_idle_timeout_s
        max_duration = self.cfg.session_max_duration_s
        # Check often enough that whichever limit is shorter is honoured
        # promptly, but not so often that an idle server spins.
        limits = [x for x in (idle_limit, max_duration) if x > 0]
        tick = min(1.0, max(0.05, min(limits) / 4)) if limits else 1.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick)
                return
            except asyncio.TimeoutError:
                pass
            idle = self.idle_seconds
            if idle_limit > 0 and idle >= idle_limit:
                log.info("idle for %.0fs -- closing billed session", idle)
                return
            if max_duration > 0 and (self._clock() - self._started_at) >= max_duration:
                log.info("session hit SESSION_MAX_DURATION (%.0fs) -- closing", max_duration)
                return
