"""Tests for the no-hardware harness itself.

The harness is what stands in for the Show until Phases 1-2 are done, so its
camera and audio behaviour has to be trustworthy. The live smoke run cannot
reach the camera path -- that needs an active Live session, which needs an API
key -- so it is covered here instead.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import protocol as P  # noqa: E402
from fake_device import DEFAULT_JPEG, DEFAULT_WAV, FakeDevice, read_pcm  # noqa: E402


class StubWebSocket:
    def __init__(self) -> None:
        self.text: list[P.Message] = []
        self.binary: list[P.MediaFrame] = []

    async def send_str(self, data: str) -> None:
        self.text.append(P.decode(data))

    async def send_bytes(self, data: bytes) -> None:
        self.binary.append(P.MediaFrame.decode(data))


def make_device(**overrides) -> tuple[FakeDevice, StubWebSocket]:
    args = argparse.Namespace(
        url="ws://127.0.0.1:8765/ws",
        secret="x",
        device_id="fake",
        wav=str(DEFAULT_WAV),
        jpeg=str(DEFAULT_JPEG),
        no_audio=False,
        tap=False,
        repeat=1,
        hold=0.0,
        shutter=False,
        stay=0,
        save_reply=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    device = FakeDevice(args)
    ws = StubWebSocket()
    device.ws = ws  # type: ignore[assignment]
    return device, ws


# --- bundled assets ---------------------------------------------------------


def test_bundled_wav_is_exactly_what_the_device_would_send() -> None:
    with wave.open(str(DEFAULT_WAV), "rb") as wav:
        assert wav.getframerate() == P.AUDIO_UP_RATE == 16_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() / wav.getframerate() > 1.0, "sample is too short to be useful"


def test_bundled_jpeg_is_a_jpeg() -> None:
    data = DEFAULT_JPEG.read_bytes()
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


def test_read_pcm_rejects_a_wav_the_device_could_not_produce(tmp_path) -> None:
    wrong = tmp_path / "44k-stereo.wav"
    with wave.open(str(wrong), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44_100)
        wav.writeframes(b"\x00" * 400)
    with pytest.raises(SystemExit) as exc:
        read_pcm(wrong)
    assert "16000 Hz mono PCM16" in str(exc.value)


# --- camera lifecycle -------------------------------------------------------


async def test_video_request_opens_the_camera_and_streams_frames() -> None:
    device, ws = make_device()
    await device._on_video(P.Video(enabled=True, fps=1.0))
    try:
        await asyncio.sleep(0.05)
        assert isinstance(ws.text[0], P.CameraStatusMsg)
        assert ws.text[0].status is P.CameraStatus.STREAMING
        assert ws.binary, "a frame should have been sent immediately"
        assert ws.binary[0].channel is P.Channel.VIDEO_UP
        assert ws.binary[0].payload[:2] == b"\xff\xd8"
    finally:
        await device._stop_camera()


async def test_video_off_releases_the_camera() -> None:
    device, ws = make_device()
    await device._on_video(P.Video(enabled=True, fps=1.0))
    await asyncio.sleep(0.05)
    await device._on_video(P.Video(enabled=False))
    sent_before = len(ws.binary)
    await asyncio.sleep(0.1)
    assert len(ws.binary) == sent_before, "no frames after release"
    assert ws.text[-1].status is P.CameraStatus.RELEASED
    assert device._camera_task is None


async def test_frames_are_capped_at_one_fps() -> None:
    device, ws = make_device()
    await device._on_video(P.Video(enabled=True, fps=1.0))
    try:
        await asyncio.sleep(0.3)
        assert len(ws.binary) == 1, "1 FPS means one frame in the first 300 ms"
    finally:
        await device._stop_camera()


async def test_closed_shutter_reports_instead_of_streaming_black() -> None:
    """HANDOFF 8.3: with the privacy latch engaged the camera stays enumerated
    and returns black frames. Say so rather than feeding black to the model."""
    device, ws = make_device(shutter=True)
    await device._on_video(P.Video(enabled=True, fps=1.0))
    await asyncio.sleep(0.1)
    assert ws.text[0].status is P.CameraStatus.SHUTTER_CLOSED
    assert ws.binary == [], "no frames may be sent with the shutter closed"
    assert device._camera_task is None


# --- control handling -------------------------------------------------------


async def test_ping_is_answered_with_a_matching_pong() -> None:
    device, ws = make_device()
    await device._on_text(P.Ping(nonce=17).encode())
    assert isinstance(ws.text[0], P.Pong) and ws.text[0].nonce == 17


async def test_mic_message_gates_the_uplink() -> None:
    device, _ws = make_device()
    assert device.mic_open is True
    await device._on_text(P.Mic(enabled=False).encode())
    assert device.mic_open is False, "uplink must stop while the server speaks"
    await device._on_text(P.Mic(enabled=True).encode())
    assert device.mic_open is True


async def test_interrupt_is_counted_as_a_playback_flush() -> None:
    device, _ws = make_device()
    await device._on_text(P.Interrupt().encode())
    assert device.interrupts == 1


async def test_display_and_clear_are_tracked() -> None:
    device, _ws = make_device()
    await device._on_text(
        P.Display(
            command=P.DisplayCommand(type=P.DisplayType.TEXT, payload={"text": "hi"})
        ).encode()
    )
    assert device.displays == 1
    await device._on_text(P.DisplayClear().encode())  # must not raise


async def test_stay_is_sent_when_the_quiet_window_is_announced() -> None:
    """``--stay N`` is how the tap-to-stay path gets smoke-tested without a
    touchscreen: each announced window costs one tap, then they run out."""
    device, ws = make_device(stay=1)
    await device._on_text(P.SessionQuiet(active=True, closes_in_s=8.0).encode())
    assert isinstance(ws.text[0], P.Stay)
    assert device.quiet_windows == 1

    await device._on_text(P.SessionQuiet(active=True, closes_in_s=8.0).encode())
    assert len(ws.text) == 1, "only --stay taps are available, and they were used"
    assert device.quiet_windows == 2


async def test_cancelled_quiet_window_is_not_tapped() -> None:
    device, ws = make_device(stay=1)
    await device._on_text(P.SessionQuiet(active=False, closes_in_s=0.0).encode())
    assert ws.text == [], "a window that was cancelled needs no tap"
    assert device.stays_left == 1


async def test_undecodable_message_does_not_crash_the_harness() -> None:
    device, _ws = make_device()
    await device._on_text("{{{not json")


def test_model_audio_is_collected_for_playback() -> None:
    device, _ws = make_device()
    frame = P.MediaFrame(channel=P.Channel.AUDIO_DOWN, payload=b"\x01\x02" * 240)
    device._on_binary(frame.encode())
    assert bytes(device.reply_audio) == b"\x01\x02" * 240
    assert device.reply_chunks == 1
