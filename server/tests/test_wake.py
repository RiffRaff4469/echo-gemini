"""Wake-word framing/gating and the energy VAD, against synthetic frames.

No ONNX model is loaded here; the detector is injected. What is under test is
the logic that surrounds it -- reframing an arbitrary byte stream into the
80 ms chunks openWakeWord requires, thresholding, and the refractory window
that stops one utterance firing twice.
"""

from __future__ import annotations

import numpy as np
import pytest

from wake import (
    OWW_CHUNK_BYTES,
    OWW_CHUNK_SAMPLES,
    DisabledDetector,
    EnergyVad,
    WakeWordEngine,
    build_detector,
    pcm_duration_s,
)


class ScriptedDetector:
    """Returns scores from a list, one per chunk; 0.0 once exhausted."""

    def __init__(self, scores: list[float]) -> None:
        self.name = "scripted"
        self.scores = list(scores)
        self.chunks_seen = 0
        self.resets = 0

    def score(self, chunk: np.ndarray) -> float:
        assert chunk.dtype == np.int16
        assert chunk.shape == (OWW_CHUNK_SAMPLES,)
        self.chunks_seen += 1
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self) -> None:
        self.resets += 1


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def silence(chunks: int = 1) -> bytes:
    return b"\x00" * (OWW_CHUNK_BYTES * chunks)


def test_engine_reframes_arbitrary_byte_sizes_into_80ms_chunks() -> None:
    det = ScriptedDetector([])
    engine = WakeWordEngine(det, threshold=0.5)
    # Feed in 640-byte (20 ms) device frames -- exactly what the client sends.
    for _ in range(4):
        engine.feed(b"\x00" * 640)
    assert det.chunks_seen == 1  # 4 x 20 ms == one 80 ms chunk
    engine.feed(b"\x00" * 640)
    assert det.chunks_seen == 1  # partial chunk buffered, not scored


def test_partial_bytes_are_buffered_across_calls() -> None:
    det = ScriptedDetector([])
    engine = WakeWordEngine(det, threshold=0.5)
    engine.feed(b"\x00" * (OWW_CHUNK_BYTES - 1))
    assert det.chunks_seen == 0
    engine.feed(b"\x00")
    assert det.chunks_seen == 1


def test_empty_feed_is_a_noop() -> None:
    det = ScriptedDetector([])
    engine = WakeWordEngine(det, threshold=0.5)
    assert engine.feed(b"") is None
    assert det.chunks_seen == 0


def test_fires_only_above_threshold() -> None:
    det = ScriptedDetector([0.1, 0.49])
    engine = WakeWordEngine(det, threshold=0.5)
    assert engine.feed(silence(2)) is None
    assert engine.last_score == pytest.approx(0.49)


def test_fires_when_threshold_is_crossed() -> None:
    det = ScriptedDetector([0.2, 0.91])
    engine = WakeWordEngine(det, threshold=0.5)
    event = engine.feed(silence(2))
    assert event is not None
    assert event.model == "scripted"
    assert event.score == pytest.approx(0.91)


def test_refractory_window_suppresses_a_second_trigger() -> None:
    clock = FakeClock()
    det = ScriptedDetector([0.9, 0.9, 0.9])
    engine = WakeWordEngine(det, threshold=0.5, refractory_s=2.0, clock=clock)

    assert engine.feed(silence(1)) is not None
    clock.advance(0.5)
    assert engine.feed(silence(1)) is None, "same utterance must not re-fire"
    clock.advance(2.0)
    assert engine.feed(silence(1)) is not None, "next utterance may fire"


def test_only_one_event_per_feed_call() -> None:
    clock = FakeClock()
    det = ScriptedDetector([0.9, 0.95, 0.99])
    engine = WakeWordEngine(det, threshold=0.5, refractory_s=0.0, clock=clock)
    event = engine.feed(silence(3))
    assert event is not None and event.score == pytest.approx(0.9)


def test_reset_drops_buffer_and_detector_state() -> None:
    det = ScriptedDetector([])
    engine = WakeWordEngine(det, threshold=0.5)
    engine.feed(b"\x00" * (OWW_CHUNK_BYTES - 2))
    engine.reset()
    assert det.resets == 1
    engine.feed(b"\x00\x00")
    # Buffer was cleared, so those 2 bytes cannot complete the old chunk.
    assert det.chunks_seen == 0


def test_disabled_detector_never_fires_but_server_keeps_running() -> None:
    engine = WakeWordEngine(DisabledDetector("test"), threshold=0.01)
    assert engine.feed(silence(10)) is None
    assert engine.active is False


def test_build_detector_degrades_instead_of_raising() -> None:
    assert isinstance(build_detector(enabled=False, model_name="alexa"), DisabledDetector)
    # A model that cannot possibly exist must not stop the server from booting.
    detector = build_detector(
        enabled=True, model_name="definitely-not-a-real-model", download=False
    )
    assert isinstance(detector, DisabledDetector)


# --- energy VAD -------------------------------------------------------------


def tone(amplitude: int, samples: int = 320) -> bytes:
    values = np.full(samples, amplitude, dtype="<i2")
    return values.tobytes()


def test_vad_reports_speech_above_threshold() -> None:
    vad = EnergyVad(threshold_rms=500.0)
    assert vad.feed(tone(4000)) is True
    assert vad.last_rms == pytest.approx(4000.0)


def test_vad_hangover_keeps_speech_true_briefly_after_it_stops() -> None:
    clock = FakeClock()
    vad = EnergyVad(threshold_rms=500.0, hangover_s=0.8, clock=clock)
    assert vad.feed(tone(4000)) is True
    clock.advance(0.5)
    assert vad.feed(tone(0)) is True, "still inside the hangover tail"
    clock.advance(0.5)
    assert vad.feed(tone(0)) is False


def test_vad_seconds_since_speech_drives_idle_close() -> None:
    clock = FakeClock()
    vad = EnergyVad(threshold_rms=500.0, clock=clock)
    vad.feed(tone(9000))
    clock.advance(130.0)
    assert vad.seconds_since_speech == pytest.approx(130.0)


def test_vad_starts_with_no_speech_history() -> None:
    # A fresh session must not inherit a "recently spoke" state, or the first
    # second of silence would look like speech and hold the session open.
    vad = EnergyVad()
    assert vad.feed(tone(0)) is False
    assert vad.seconds_since_speech == float("inf")


def test_vad_handles_odd_length_and_empty_buffers() -> None:
    assert EnergyVad.rms(b"") == 0.0
    assert EnergyVad.rms(b"\x01") == 0.0  # half a sample
    assert EnergyVad.rms(b"\x00\x10\x7f") == pytest.approx(4096.0)


def test_vad_reset_clears_history() -> None:
    vad = EnergyVad()
    vad.feed(tone(9000))
    vad.reset()
    assert vad.last_rms == 0.0
    assert vad.seconds_since_speech == float("inf")


def test_pcm_duration() -> None:
    assert pcm_duration_s(b"\x00" * 640) == pytest.approx(0.02)  # 20 ms @16 kHz
