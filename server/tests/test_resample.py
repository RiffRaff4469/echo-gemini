"""The 44.1 kHz stereo -> 24 kHz mono converter.

The property that actually matters here is chunk independence. librespot's PCM
arrives from a pipe, which splits wherever the OS feels like it -- mid-sample,
mid-frame, in 3-byte dribbles -- and a converter that produces different audio
depending on where the splits fell would produce clicks nobody could reproduce.
Several tests below are the same signal fed differently, asserting the output is
byte-identical.
"""

from __future__ import annotations

import math
import struct

import pytest

from resample import PcmConverter


def stereo(samples: list[tuple[int, int]]) -> bytes:
    return b"".join(struct.pack("<hh", l, r) for l, r in samples)


def mono_bytes(samples: list[int]) -> bytes:
    return b"".join(struct.pack("<h", s) for s in samples)


def unpack(raw: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


def tone(hz: float, seconds: float, rate: int = 44_100, amp: int = 12_000) -> bytes:
    count = int(rate * seconds)
    return stereo(
        [
            (
                int(amp * math.sin(2 * math.pi * hz * n / rate)),
                int(amp * math.sin(2 * math.pi * hz * n / rate)),
            )
            for n in range(count)
        ]
    )


# --- shape ------------------------------------------------------------------


def test_output_rate_is_correct_over_a_second():
    conv = PcmConverter()
    out = conv.feed(tone(440, 1.0))
    # 44100 in at 147/80 gives 24000 out, give or take the sample the last
    # interpolation is still waiting for.
    assert 23_995 <= len(out) // 2 <= 24_000


def test_ratio_is_exact_rational_not_float():
    conv = PcmConverter()
    assert (conv._num, conv._den) == (147, 80)


def test_channels_are_averaged_not_summed():
    """Summing clips anything mastered near full scale, which is most of Spotify."""
    conv = PcmConverter(in_rate=24_000, in_channels=2, out_rate=24_000)
    out = unpack(conv.feed(stereo([(30_000, 30_000)] * 8)))
    assert out and max(out) == 30_000


def test_a_constant_signal_stays_constant():
    """Linear interpolation between equal samples must not wobble."""
    conv = PcmConverter()
    out = unpack(conv.feed(stereo([(1_000, 1_000)] * 4_410)))
    assert out
    assert set(out) == {1_000}


def test_amplitude_survives_a_tone():
    conv = PcmConverter()
    out = unpack(conv.feed(tone(440, 0.5, amp=12_000)))
    peak = max(abs(s) for s in out)
    # Linear interpolation between adjacent samples of a 440 Hz tone loses a
    # fraction of a percent, not a decibel.
    assert 11_800 <= peak <= 12_000


def test_mono_input_is_not_downmixed():
    conv = PcmConverter(in_rate=44_100, in_channels=1, out_rate=24_000)
    out = unpack(conv.feed(mono_bytes([5_000] * 4_410)))
    assert out and set(out) == {5_000}


def test_matching_rates_and_mono_is_passthrough():
    conv = PcmConverter(in_rate=24_000, in_channels=1, out_rate=24_000)
    assert conv.passthrough
    payload = mono_bytes([1, -2, 3, -4])
    assert conv.feed(payload) == payload


# --- chunking ---------------------------------------------------------------


@pytest.mark.parametrize("chunk", [3, 4, 7, 64, 1_000, 8_820])
def test_chunking_does_not_change_the_output(chunk):
    source = tone(440, 0.2)

    whole = PcmConverter().feed(source)

    split = PcmConverter()
    pieces = [
        split.feed(source[i : i + chunk]) for i in range(0, len(source), chunk)
    ]
    assert b"".join(pieces) == whole


def test_a_partial_sample_frame_is_held_not_dropped():
    """A 3-byte read cannot make a sample; it must wait, not vanish."""
    conv = PcmConverter()
    assert conv.feed(b"\x01\x02\x03") == b""
    # Feeding the rest of that frame plus enough to interpolate against
    # produces audio built from all of it.
    rest = b"\x04" + stereo([(100, 100)] * 200)
    assert conv.feed(rest) != b""


def test_empty_input_is_harmless():
    conv = PcmConverter()
    assert conv.feed(b"") == b""


def test_reset_forgets_the_carry():
    conv = PcmConverter()
    conv.feed(b"\x01\x02\x03")
    conv.reset()
    # With the stray byte forgotten, this is the same as a fresh converter.
    fresh = PcmConverter()
    payload = tone(440, 0.05)
    assert conv.feed(payload) == fresh.feed(payload)


# --- guards -----------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"in_rate": 0},
        {"out_rate": -1},
        {"in_channels": 0},
        {"in_channels": 3},
    ],
)
def test_nonsense_formats_are_rejected(kwargs):
    with pytest.raises(ValueError):
        PcmConverter(**kwargs)


def test_full_scale_input_cannot_overflow_int16():
    """Interpolating between the extremes must stay inside the range it packs."""
    conv = PcmConverter()
    alternating = stereo(
        [(-32_768, -32_768) if n % 2 else (32_767, 32_767) for n in range(4_410)]
    )
    out = unpack(conv.feed(alternating))
    assert out
    assert min(out) >= -32_768 and max(out) <= 32_767
