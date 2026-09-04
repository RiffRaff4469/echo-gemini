"""Wake word detection over the inbound 16 kHz stream.

Wake word runs here rather than on the device (HANDOFF section 7): it sidesteps
32-bit Android native libraries, costs the Show zero CPU, and -- the reason that
actually decides it -- lets us retune against a weak single microphone by
replaying logged audio, without reflashing anything.

The device already applies a VAD gate, so silence never reaches us. What arrives
is a stream of 20 ms PCM16 frames that this module reframes into the 80 ms
(1280-sample) chunks openWakeWord expects, scores, and thresholds with a
refractory period so one utterance cannot fire twice.

``EnergyVad`` is a second, independent thing in this file: a cheap RMS speech
detector the session manager uses to decide whether the user is still talking,
which is what drives the idle-close timer in ``gemini_live``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from protocol import AUDIO_SAMPLE_WIDTH, AUDIO_UP_RATE

log = logging.getLogger("echo.wake")

# openWakeWord consumes 1280 samples (80 ms @ 16 kHz) per predict() call.
OWW_CHUNK_SAMPLES = 1280
OWW_CHUNK_BYTES = OWW_CHUNK_SAMPLES * AUDIO_SAMPLE_WIDTH


class Detector(Protocol):
    """Scores a fixed-size chunk of PCM16. Returns confidence in [0, 1]."""

    name: str

    def score(self, chunk: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class DisabledDetector:
    """Used when ``WAKE_ENABLED=false`` or the ONNX models could not be loaded.

    This is a *documented* no-op, not a silent stub: it logs once at startup and
    scores every chunk 0.0, so the voice loop still works end to end through
    tap-to-talk (which HANDOFF section 1 keeps as a permanent override anyway).
    """

    def __init__(self, reason: str) -> None:
        self.name = "disabled"
        self.reason = reason
        log.warning(
            "wake word DISABLED (%s) -- tap-to-talk still opens a session", reason
        )

    def score(self, chunk: np.ndarray) -> float:
        return 0.0

    def reset(self) -> None:
        pass


class OpenWakeWordDetector:
    """openWakeWord via ONNX Runtime.

    ``alexa`` is the default: it is the best-trained of the bundled models and
    this hardware's users will say it out of muscle memory anyway. Set
    ``WAKE_MODEL`` to any other bundled name (``hey_jarvis``, ``hey_mycroft``,
    ``hey_rhasspy``) or to a path to a custom ``.onnx``.
    """

    def __init__(self, model_name: str, *, download: bool = True) -> None:
        import openwakeword  # imported lazily: heavy, and optional at runtime
        from openwakeword.model import Model

        if download:
            # Idempotent; a no-op once the ONNX files are in the package dir.
            try:
                openwakeword.utils.download_models()
            except Exception as exc:  # network blip, corporate proxy, ...
                log.warning(
                    "openwakeword.download_models() failed (%s); continuing with "
                    "whatever is already on disk",
                    exc,
                )

        self.name = model_name
        self._model = Model(
            wakeword_models=[model_name], inference_framework="onnx"
        )
        # Model keys are basenames, not the path we may have been given.
        self._keys = list(self._model.models.keys())
        log.info("openWakeWord ready: models=%s (onnx)", self._keys)

    def score(self, chunk: np.ndarray) -> float:
        prediction = self._model.predict(chunk)
        return max(prediction.values()) if prediction else 0.0

    def reset(self) -> None:
        self._model.reset()


def build_detector(
    *, enabled: bool, model_name: str, download: bool = True
) -> Detector:
    """Pick a detector, degrading to ``DisabledDetector`` with a reason.

    A missing ONNX model must never stop the server from booting -- the display
    API and the ambient link are independent of it.
    """
    if not enabled:
        return DisabledDetector("WAKE_ENABLED=false")
    try:
        return OpenWakeWordDetector(model_name, download=download)
    except Exception as exc:
        return DisabledDetector(f"could not load model {model_name!r}: {exc}")


@dataclass
class WakeEvent:
    model: str
    score: float
    at: float


class WakeWordEngine:
    """Reframes the inbound stream, scores it, and applies hysteresis.

    Call ``feed()`` with raw PCM16 bytes of any size; it buffers and emits at
    most one ``WakeEvent`` per ``refractory_s`` window.
    """

    def __init__(
        self,
        detector: Detector,
        *,
        threshold: float = 0.5,
        refractory_s: float = 2.0,
        clock=time.monotonic,
    ) -> None:
        self.detector = detector
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._clock = clock
        self._buffer = bytearray()
        self._last_fire = float("-inf")
        self.last_score = 0.0

    @property
    def active(self) -> bool:
        return not isinstance(self.detector, DisabledDetector)

    def feed(self, pcm: bytes) -> WakeEvent | None:
        """Consume PCM16 bytes. Returns a ``WakeEvent`` on the trigger chunk.

        Only the first trigger in a refractory window is returned; later chunks
        in the same utterance are scored (so ``last_score`` stays live) but
        suppressed.
        """
        if not pcm:
            return None
        self._buffer.extend(pcm)

        event: WakeEvent | None = None
        while len(self._buffer) >= OWW_CHUNK_BYTES:
            raw = bytes(self._buffer[:OWW_CHUNK_BYTES])
            del self._buffer[:OWW_CHUNK_BYTES]
            chunk = np.frombuffer(raw, dtype="<i2")
            score = float(self.detector.score(chunk))
            self.last_score = score
            if score < self.threshold:
                continue
            now = self._clock()
            if now - self._last_fire < self.refractory_s:
                continue
            self._last_fire = now
            if event is None:
                event = WakeEvent(
                    model=self.detector.name, score=score, at=time.time()
                )
                log.info("wake word %r fired (score=%.3f)", self.detector.name, score)
        return event

    def reset(self) -> None:
        """Drop buffered audio and detector history.

        Called when a session opens so the wake phrase itself does not linger in
        the ring buffer and re-trigger, and on device reconnect so a gap in the
        stream is not scored as continuous audio.
        """
        self._buffer.clear()
        self.last_score = 0.0
        self.detector.reset()


# ---------------------------------------------------------------------------
# Energy VAD -- drives the idle-close timer
# ---------------------------------------------------------------------------


class EnergyVad:
    """RMS-over-threshold speech detector with hangover.

    Deliberately crude. The device already gates silence; this exists only so
    the server can distinguish "the user is still in a conversation" from "the
    device is streaming room noise", which is what decides when to close a
    billed Live session (HANDOFF section 12).
    """

    def __init__(
        self,
        *,
        threshold_rms: float = 500.0,
        hangover_s: float = 0.8,
        clock=time.monotonic,
    ) -> None:
        self.threshold_rms = threshold_rms
        self.hangover_s = hangover_s
        self._clock = clock
        self._last_speech = float("-inf")
        self.last_rms = 0.0

    @staticmethod
    def rms(pcm: bytes) -> float:
        if len(pcm) < AUDIO_SAMPLE_WIDTH:
            return 0.0
        samples = np.frombuffer(
            pcm[: len(pcm) - (len(pcm) % AUDIO_SAMPLE_WIDTH)], dtype="<i2"
        )
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

    def feed(self, pcm: bytes) -> bool:
        """Returns True while speech is present (including the hangover tail)."""
        self.last_rms = self.rms(pcm)
        now = self._clock()
        if self.last_rms >= self.threshold_rms:
            self._last_speech = now
            return True
        return (now - self._last_speech) < self.hangover_s

    @property
    def seconds_since_speech(self) -> float:
        return self._clock() - self._last_speech

    def reset(self) -> None:
        self._last_speech = float("-inf")
        self.last_rms = 0.0


def pcm_duration_s(pcm: bytes, rate: int = AUDIO_UP_RATE) -> float:
    return len(pcm) / (rate * AUDIO_SAMPLE_WIDTH)
