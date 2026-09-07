"""PCM format conversion, for audio that does not already arrive in the
device's format.

Everything else in this server is pass-through: the device captures at exactly
the 16 kHz the Live API wants and plays back at exactly the 24 kHz it emits
(``gemini_live`` module docstring). librespot is the first source that does not
play along -- it decodes Spotify's Ogg/Vorbis to CD audio, 44.1 kHz stereo
PCM16, and something has to bridge that to the 24 kHz mono the device's
``AudioPlayback`` is pinned to.

The quality bar is a bedside speaker, not mastering. Channels are averaged and
samples are linearly interpolated; there is no anti-alias filter, so content
above 12 kHz folds back. On a single small speaker driven from a phone-grade
DAC that is inaudible next to everything else in the chain, and the alternative
-- a windowed-sinc polyphase filter, or a native dependency -- costs far more
than it buys here.

Stdlib only, and deliberately so. ``numpy`` is already installed for the wake
word, but this runs on every 50 ms of music for hours at a time and must keep
working if the wake word is ever unbundled. ``array`` does the per-sample
unpacking in C, which is the part that would actually hurt in a Python loop.
"""

from __future__ import annotations

import sys
from array import array
from math import gcd

# 44.1 kHz stereo PCM16 is what ``librespot --backend pipe --format S16``
# writes. Both are configurable on the ``PcmConverter`` because the flag is
# configurable on librespot, and a mismatch here is silent garbage rather than
# an error.
LIBRESPOT_RATE = 44_100
LIBRESPOT_CHANNELS = 2

_NATIVE_BIG_ENDIAN = sys.byteorder == "big"


class PcmConverter:
    """Streaming PCM16 rate/channel converter, safe to call chunk by chunk.

    Two pieces of state make this correct across chunk boundaries, and both
    matter: the trailing bytes of a chunk that did not complete a sample frame
    (a pipe splits wherever it likes, not on 4-byte boundaries), and the last
    sample of the previous chunk, which the first interpolation of the next
    chunk needs to sit between.

    Resampling positions are tracked as an exact rational -- for 44100 -> 24000
    that is 147/80 -- rather than a float. Accumulating a float step for hours
    of playback drifts; integers do not.
    """

    def __init__(
        self,
        *,
        in_rate: int = LIBRESPOT_RATE,
        in_channels: int = LIBRESPOT_CHANNELS,
        out_rate: int = 24_000,
    ) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        if in_channels not in (1, 2):
            raise ValueError("in_channels must be 1 or 2")

        self.in_rate = in_rate
        self.in_channels = in_channels
        self.out_rate = out_rate

        self._frame_bytes = in_channels * 2
        step = gcd(in_rate, out_rate)
        self._num = in_rate // step  # 147 for 44100 -> 24000
        self._den = out_rate // step  # 80
        self.reset()

    @property
    def passthrough(self) -> bool:
        """True when the input already is the output format."""
        return self.in_rate == self.out_rate and self.in_channels == 1

    def reset(self) -> None:
        """Forget everything carried across chunks.

        Called between tracks and on every librespot restart: interpolating the
        first sample of a new stream against the last sample of the old one
        produces one wrong sample, which is inaudible, but leaving a stale
        partial frame in the buffer shifts the byte alignment of an entire
        track, which is not.
        """
        self._tail = b""
        # The mono sample immediately before the next chunk's first sample.
        self._prev = 0
        # Position of the next output sample, as an index into the incoming
        # mono stream relative to the start of the next chunk, plus a fraction
        # in units of 1/_den. The invariant is _cursor >= -1: never further
        # back than _prev, which is the only history kept.
        self._cursor = 0
        self._frac = 0

    def feed(self, raw: bytes) -> bytes:
        """Convert one chunk. Returns the bytes ready to send, possibly empty.

        An empty return is normal, not a failure: a chunk shorter than a couple
        of input samples cannot produce an output sample yet, and its bytes are
        held until the next call.
        """
        data = self._tail + raw if self._tail else raw
        usable = len(data) - (len(data) % self._frame_bytes)
        self._tail = data[usable:]
        if usable == 0:
            return b""

        samples = array("h")
        samples.frombytes(data[:usable])
        if _NATIVE_BIG_ENDIAN:
            # The wire is little-endian PCM16; array('h') is native-endian.
            samples.byteswap()

        mono = self._downmix(samples)
        out = self._resample(mono)

        if _NATIVE_BIG_ENDIAN:
            out.byteswap()
        return out.tobytes()

    # --- internals ----------------------------------------------------------

    def _downmix(self, samples: array) -> array:
        if self.in_channels == 1:
            return samples
        # Averaging rather than summing: two channels summed clip on anything
        # mastered near full scale, which is most of Spotify.
        left = samples[0::2]
        right = samples[1::2]
        return array("h", [(l + r) // 2 for l, r in zip(left, right)])

    def _resample(self, mono: array) -> array:
        if self.in_rate == self.out_rate:
            return mono

        n = len(mono)
        num = self._num
        den = self._den
        cursor = self._cursor
        frac = self._frac
        prev = self._prev
        out = array("h")
        append = out.append

        # An output sample needs the input samples on either side of it, so the
        # loop stops one short of the end of the chunk; the rest is picked up
        # next time against _prev.
        while cursor <= n - 2:
            a = prev if cursor < 0 else mono[cursor]
            b = mono[cursor + 1]
            append(a + ((b - a) * frac) // den)
            frac += num
            cursor += frac // den
            frac %= den

        # The loop leaves cursor at n-1 or n (the step is 147/80, so it can
        # advance by two). Rebasing keeps it in [-1, 0] rather than counting up
        # forever, and preserves the >= -1 invariant either way.
        self._cursor = cursor - n
        self._frac = frac
        self._prev = mono[n - 1]
        return out
