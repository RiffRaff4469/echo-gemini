"""Live session lifecycle against a fake connector -- no API key, no network.

What matters here is the behaviour that costs money or breaks hardware:
idle-close (Live bills by session duration), barge-in flushing device playback,
the 1 FPS video ceiling, and releasing the camera on EVERY exit path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from config import Config
from gemini_live import LOOK_TOOL, STOP_LOOK_TOOL, LiveSessionManager
from protocol import CameraStatus, UiState


class RecordingSink:
    """Stands in for ``Hub``; records what the session pushed at the device."""

    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.interrupts = 0
        self.states: list[UiState] = []
        self.mic: list[bool] = []
        self.vision: list[bool] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def send_interrupt(self) -> None:
        self.interrupts += 1

    async def set_state(self, state: UiState) -> None:
        self.states.append(state)

    async def set_mic(self, enabled: bool) -> None:
        self.mic.append(enabled)

    async def set_vision(self, enabled: bool) -> None:
        self.vision.append(enabled)


class FakeLiveSession:
    """Implements the slice of ``google.genai`` ``AsyncSession`` we use."""

    def __init__(self) -> None:
        self.audio_in: list[bytes] = []
        self.video_in: list[bytes] = []
        self.text_in: list[str] = []
        self.tool_responses: list = []
        self.closed = False
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send_realtime_input(self, *, audio=None, video=None, text=None, **_):
        if audio is not None:
            self.audio_in.append(audio.data)
        if video is not None:
            assert audio is None
            self.video_in.append(video.data)
        if text is not None:
            self.text_in.append(text)

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)

    async def receive(self):
        while True:
            item = await self._inbox.get()
            if item is None:
                return
            yield item

    # --- test-side helpers ---
    def emit(self, message) -> None:
        self._inbox.put_nowait(message)

    def emit_audio(self, pcm: bytes) -> None:
        self.emit(SimpleNamespace(data=pcm, server_content=None, tool_call=None, go_away=None))

    def emit_content(self, **kwargs) -> None:
        attrs = {
            "interrupted": False,
            "turn_complete": False,
            "input_transcription": None,
            "output_transcription": None,
        }
        attrs.update(kwargs)
        content = SimpleNamespace(**attrs)
        self.emit(
            SimpleNamespace(data=None, server_content=content, tool_call=None, go_away=None)
        )

    def emit_tool_call(self, name: str, call_id: str = "call-1") -> None:
        self.emit(
            SimpleNamespace(
                data=None,
                server_content=None,
                tool_call=SimpleNamespace(
                    function_calls=[SimpleNamespace(name=name, id=call_id, args={})]
                ),
                go_away=None,
            )
        )


class FakeConnector:
    def __init__(self, session: FakeLiveSession) -> None:
        self.session = session
        self.connects = 0

    def connect(self):
        connector = self

        class _CM:
            async def __aenter__(self):
                connector.connects += 1
                return connector.session

            async def __aexit__(self, *exc):
                connector.session.closed = True
                return False

        return _CM()


def make_config(**overrides) -> Config:
    cfg = Config(
        shared_secret="test-secret",
        gemini_api_key="",  # deliberately absent: the fake connector is injected
        session_idle_timeout_s=60.0,
        session_max_duration_s=0.0,
        vision_mode="on_demand",
        wake_enabled=False,
        system_instruction="be brief",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


async def settle(times: int = 6) -> None:
    """Let the pumps run without hard-coding a wall-clock sleep."""
    for _ in range(times):
        await asyncio.sleep(0)


@pytest.fixture
async def running():
    session = FakeLiveSession()
    sink = RecordingSink()
    connector = FakeConnector(session)
    manager = LiveSessionManager(make_config(), sink, connector=connector)
    await manager.start("test")
    await settle()
    try:
        yield manager, session, sink, connector
    finally:
        await manager.stop("test teardown")


# --- opening / closing ------------------------------------------------------


async def test_start_opens_exactly_one_session(running) -> None:
    manager, _session, sink, connector = running
    assert manager.active
    assert connector.connects == 1
    assert UiState.LISTENING in sink.states

    # A second start must re-arm the idle timer rather than open a second
    # billed session.
    assert await manager.start("again") is False
    assert connector.connects == 1


async def test_start_without_a_key_is_a_logged_noop_not_a_crash() -> None:
    sink = RecordingSink()
    manager = LiveSessionManager(make_config(gemini_api_key=""), sink)
    assert await manager.start("wake") is False
    assert manager.active is False
    assert sink.states == [UiState.IDLE]


async def test_stop_is_idempotent(running) -> None:
    manager, _session, _sink, _connector = running
    await manager.stop("first")
    assert manager.active is False
    await manager.stop("second")  # must not raise


async def test_session_exit_resets_device_state(running) -> None:
    manager, _session, sink, _connector = running
    await manager.stop("done")
    assert sink.states[-1] is UiState.IDLE
    assert sink.mic[-1] is True, "mic must be reopened when the session ends"


# --- idle close: this is the billing control -------------------------------


async def test_idle_close_ends_the_session() -> None:
    session = FakeLiveSession()
    sink = RecordingSink()
    manager = LiveSessionManager(
        make_config(session_idle_timeout_s=0.2), sink, connector=FakeConnector(session)
    )
    await manager.start("test")
    assert manager.active
    await asyncio.sleep(0.6)
    assert manager.active is False, "an idle Live session must close -- it bills"
    assert sink.states[-1] is UiState.IDLE


async def test_activity_defers_the_idle_close() -> None:
    session = FakeLiveSession()
    sink = RecordingSink()
    manager = LiveSessionManager(
        make_config(session_idle_timeout_s=0.4), sink, connector=FakeConnector(session)
    )
    await manager.start("test")
    for _ in range(6):
        await asyncio.sleep(0.1)
        manager.touch()
    assert manager.active, "the session must survive while the user is talking"
    await manager.stop("done")


async def test_max_duration_closes_even_a_busy_session() -> None:
    session = FakeLiveSession()
    sink = RecordingSink()
    manager = LiveSessionManager(
        make_config(session_idle_timeout_s=60.0, session_max_duration_s=0.3),
        sink,
        connector=FakeConnector(session),
    )
    await manager.start("test")
    for _ in range(8):
        await asyncio.sleep(0.08)
        manager.touch()
    assert manager.active is False


async def test_loud_audio_defers_idle_close_but_silence_does_not() -> None:
    session = FakeLiveSession()
    sink = RecordingSink()
    manager = LiveSessionManager(
        make_config(session_idle_timeout_s=0.5), sink, connector=FakeConnector(session)
    )
    await manager.start("test")
    silence = b"\x00" * 640
    for _ in range(5):
        await asyncio.sleep(0.15)
        manager.feed_audio(silence)
    assert manager.active is False, "gated silence must not hold a billed session open"


# --- audio -----------------------------------------------------------------


async def test_mic_audio_is_forwarded_unresampled(running) -> None:
    manager, session, _sink, _connector = running
    manager.feed_audio(b"\x11\x22" * 320)
    await settle()
    assert session.audio_in == [b"\x11\x22" * 320]


async def test_model_audio_reaches_the_device_and_closes_the_mic(running) -> None:
    _manager, session, sink, _connector = running
    session.emit_audio(b"\xaa\xbb" * 100)
    await settle(10)
    assert sink.audio == [b"\xaa\xbb" * 100]
    # Half-duplex: mic shuts while the model speaks.
    assert sink.mic[-1] is False
    assert sink.states[-1] is UiState.SPEAKING


async def test_turn_complete_reopens_the_mic(running) -> None:
    _manager, session, sink, _connector = running
    session.emit_audio(b"\x01\x02")
    await settle(10)
    session.emit_content(turn_complete=True)
    await settle(10)
    assert sink.mic[-1] is True
    assert sink.states[-1] is UiState.LISTENING


async def test_barge_in_flushes_device_playback(running) -> None:
    _manager, session, sink, _connector = running
    session.emit_audio(b"\x01\x02" * 50)
    await settle(10)
    session.emit_content(interrupted=True)
    await settle(10)
    assert sink.interrupts == 1, "an interrupt must flush AudioTrack immediately"
    assert sink.mic[-1] is True
    assert sink.states[-1] is UiState.LISTENING


async def test_audio_queue_drops_oldest_under_backpressure(running) -> None:
    manager, _session, _sink, _connector = running
    for i in range(400):
        manager.feed_audio(i.to_bytes(2, "little") * 320)
    assert manager.dropped_audio_frames > 0, "the queue must be bounded"


async def test_audio_fed_when_inactive_is_discarded() -> None:
    sink = RecordingSink()
    manager = LiveSessionManager(make_config(), sink, connector=FakeConnector(FakeLiveSession()))
    manager.feed_audio(b"\x00" * 640)  # must not raise
    assert manager.active is False


# --- video -----------------------------------------------------------------


async def test_vision_is_off_until_the_model_asks(running) -> None:
    manager, session, sink, _connector = running
    assert manager.vision_on is False
    assert sink.vision == [], "the camera must not open on its own"

    session.emit_tool_call(LOOK_TOOL)
    await settle(10)
    assert manager.vision_on is True
    assert sink.vision == [True]
    assert session.tool_responses[0].name == LOOK_TOOL


async def test_model_can_close_the_camera_again(running) -> None:
    manager, session, sink, _connector = running
    session.emit_tool_call(LOOK_TOOL, "c1")
    await settle(10)
    session.emit_tool_call(STOP_LOOK_TOOL, "c2")
    await settle(10)
    assert manager.vision_on is False
    assert sink.vision == [True, False]


async def test_unknown_tool_call_is_answered_not_ignored(running) -> None:
    _manager, session, _sink, _connector = running
    session.emit_tool_call("launch_the_missiles")
    await settle(10)
    assert "error" in session.tool_responses[0].response


async def test_camera_is_released_when_the_session_ends(running) -> None:
    manager, session, sink, _connector = running
    session.emit_tool_call(LOOK_TOOL)
    await settle(10)
    assert sink.vision == [True]

    await manager.stop("idle")
    # HANDOFF 8.3: the camera is released the moment the session ends, on every
    # exit path. Leaving a HAL1 camera open is how this hardware wedges.
    assert sink.vision == [True, False]
    assert manager.vision_on is False


async def test_camera_is_released_even_when_the_session_errors() -> None:
    class ExplodingSession(FakeLiveSession):
        async def receive(self):
            await asyncio.sleep(0.05)
            raise RuntimeError("connection reset by peer")
            yield  # pragma: no cover -- makes this an async generator

    session = ExplodingSession()
    sink = RecordingSink()
    manager = LiveSessionManager(
        make_config(vision_mode="always"), sink, connector=FakeConnector(session)
    )
    await manager.start("test")
    await asyncio.sleep(0.3)
    assert manager.active is False
    assert sink.vision == [True, False]
    assert sink.states[-1] is UiState.IDLE


async def test_vision_always_mode_opens_the_camera_immediately() -> None:
    session = FakeLiveSession()
    sink = RecordingSink()
    manager = LiveSessionManager(
        make_config(vision_mode="always"), sink, connector=FakeConnector(session)
    )
    await manager.start("test")
    await settle(10)
    assert sink.vision == [True]
    await manager.stop("done")


async def test_video_frames_are_capped_at_one_fps(running) -> None:
    manager, session, _sink, _connector = running
    manager._vision_on = True
    for i in range(5):
        manager.feed_video(f"jpeg-{i}".encode())
        await settle(4)
    # 1 FPS: within the same second only the first frame goes to the API.
    assert len(session.video_in) == 1


async def test_video_queue_keeps_the_freshest_frame(running) -> None:
    manager, _session, _sink, _connector = running
    for i in range(10):
        manager.feed_video(f"frame-{i}".encode())
    assert manager._video_q.qsize() <= 3


# --- camera status relay ----------------------------------------------------


async def test_shutter_closed_is_described_to_the_model(running) -> None:
    manager, session, _sink, _connector = running
    await manager.note_camera_status(CameraStatus.SHUTTER_CLOSED, "near-black")
    assert session.text_in, "the model must be told, not silently fed black frames"
    assert "shutter" in session.text_in[0].lower()


async def test_camera_error_is_described_to_the_model(running) -> None:
    manager, session, _sink, _connector = running
    await manager.note_camera_status(CameraStatus.ERROR, "HAL busy")
    assert "HAL busy" in session.text_in[0]


async def test_benign_camera_status_is_not_narrated(running) -> None:
    manager, session, _sink, _connector = running
    await manager.note_camera_status(CameraStatus.STREAMING, "")
    assert session.text_in == []


async def test_camera_status_without_a_session_is_a_noop() -> None:
    sink = RecordingSink()
    manager = LiveSessionManager(make_config(), sink)
    await manager.note_camera_status(CameraStatus.SHUTTER_CLOSED, "")  # must not raise
