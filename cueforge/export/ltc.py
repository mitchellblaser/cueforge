"""SMPTE LTC (linear timecode) audio generation, plus a small decoder for tests."""
from __future__ import annotations

import numpy as np

from ..core.timecode import FrameRate, frame_count_to_tc, seconds_to_frames, tc_to_frame_count

SYNC_WORD = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]


def _bcd(value: int, bits: int) -> list[int]:
    return [(value >> i) & 1 for i in range(bits)]


def ltc_frame_bits(h: int, m: int, s: int, f: int, rate: FrameRate) -> list[int]:
    b = [0] * 80
    b[0:4] = _bcd(f % 10, 4)
    b[8:10] = _bcd(f // 10, 2)
    b[10] = 1 if rate.drop_frame else 0
    b[16:20] = _bcd(s % 10, 4)
    b[24:27] = _bcd(s // 10, 3)
    b[32:36] = _bcd(m % 10, 4)
    b[40:43] = _bcd(m // 10, 3)
    b[48:52] = _bcd(h % 10, 4)
    b[56:58] = _bcd(h // 10, 2)
    b[64:80] = SYNC_WORD
    # polarity correction bit: 27 for 24/30 fps, 59 for 25 fps
    pbit = 59 if rate.nominal == 25 else 27
    zeros = b.count(0)
    if zeros % 2 == 1:
        b[pbit] = 1
    return b


def generate_ltc(start_frame: int, n_frames: int, rate: FrameRate, sr: int = 48000,
                 level_db: float = -12.0) -> np.ndarray:
    """Return a mono float32 LTC signal starting exactly on `start_frame`."""
    amp = 10 ** (level_db / 20)
    bit_rate = rate.fps * 80
    total = int(np.ceil(n_frames * 80 * sr / bit_rate))
    out = np.empty(total, np.float32)
    level = 1.0
    pos = 0
    bit_index = 0
    for fr in range(n_frames):
        h, m, s, f = frame_count_to_tc(start_frame + fr, rate)
        for bit in ltc_frame_bits(h % 24, m, s, f, rate):
            a = int(round(bit_index * sr / bit_rate))
            c = int(round((bit_index + 1) * sr / bit_rate))
            mid = (a + c) // 2
            level = -level  # transition at every bit start
            if bit:
                out[a:mid] = level
                level = -level
                out[mid:c] = level
            else:
                out[a:c] = level
            bit_index += 1
            pos = c
    out = out[:pos]
    # ~25 us rise time (SMPTE recommends 25 +/- 5 us)
    k = max(1, int(round(25e-6 * sr * 2)))
    if k > 1:
        out = np.convolve(out, np.ones(k) / k, mode="same").astype(np.float32)
    return out * amp


def render_ltc_for_project(start_seconds: float, end_seconds: float, tc_offset: float, rate: FrameRate,
                           sr: int = 48000, level_db: float = -12.0) -> tuple[np.ndarray, float]:
    """LTC covering [start, end] of song time. Returns (signal, song time of sample 0).

    Sample 0 is placed on a frame boundary so the file can be lined up exactly.
    """
    first = seconds_to_frames(start_seconds + tc_offset, rate)
    if first / rate.fps > start_seconds + tc_offset + 1e-9:
        first -= 1
    first = max(first, 0)
    last = seconds_to_frames(end_seconds + tc_offset, rate) + 1
    sig = generate_ltc(first, last - first, rate, sr, level_db)
    return sig, first / rate.fps - tc_offset


def decode_ltc(signal: np.ndarray, sr: int, rate: FrameRate) -> list[tuple[int, int, int, int, int]]:
    """Decode LTC. Returns [(sample_index_of_frame_start, h, m, s, f), ...]."""
    sgn = np.sign(signal)
    sgn[sgn == 0] = 1
    edges = np.where(np.diff(sgn) != 0)[0] + 1
    half = sr / (rate.fps * 80) / 2
    bits: list[int] = []
    starts: list[int] = []
    i = 0
    while i < len(edges) - 1:
        d = edges[i + 1] - edges[i]
        if d < half * 1.5:  # short-short = 1
            if i + 2 < len(edges):
                bits.append(1)
                starts.append(int(edges[i]))
            i += 2
        else:
            bits.append(0)
            starts.append(int(edges[i]))
            i += 1
    frames = []
    for k in range(len(bits) - 79):
        if bits[k + 64:k + 80] == SYNC_WORD:
            b = bits[k:k + 80]
            def val(a, n):
                return sum(b[a + j] << j for j in range(n))
            f = val(0, 4) + 10 * val(8, 2)
            s = val(16, 4) + 10 * val(24, 3)
            m = val(32, 4) + 10 * val(40, 3)
            h = val(48, 4) + 10 * val(56, 2)
            frames.append((starts[k], h, m, s, f))
    return frames


__all__ = ["generate_ltc", "render_ltc_for_project", "decode_ltc", "ltc_frame_bits", "tc_to_frame_count"]
