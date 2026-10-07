"""Live SMPTE LTC: timecode audio generated block by block, locked to the playhead.

Each 80-bit LTC frame has an even number of transitions (the polarity bit sees to that), so
the signal level at any moment is a pure function of the timecode time. There is no running
state to carry between blocks, and a loop or seek simply continues at the new time.
"""
from __future__ import annotations

import numpy as np

from ..core.timecode import FrameRate, frame_count_to_tc, get_rate
from ..export.ltc import ltc_frame_bits


class LtcGenerator:
    def __init__(self) -> None:
        self.rate: FrameRate = get_rate("30")
        self.offset = 0.0            # timecode (seconds) at song time 0
        self.level_db = -12.0
        self._frames: dict[tuple[str, int], tuple[np.ndarray, np.ndarray]] = {}

    def _frame(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """(bits, transitions before each bit's middle) for timecode frame count n."""
        key = (self.rate.key, n)
        hit = self._frames.get(key)
        if hit is None:
            if len(self._frames) > 512:
                self._frames.clear()
            h, m, s, f = frame_count_to_tc(n, self.rate)
            bits = np.array(ltc_frame_bits(h % 24, m, s, f, self.rate), np.int64)
            ones_before = np.concatenate(([0], np.cumsum(bits)[:-1]))
            hit = self._frames[key] = (bits, ones_before)
        return hit

    def render(self, song_start: float, frames: int, step: float) -> np.ndarray:
        """Mono LTC for `frames` samples starting at song time `song_start`, `step` song
        seconds per sample (speed / sample rate). Silent before timecode 0."""
        t = self.offset + song_start + np.arange(frames) * step
        fp = t * self.rate.fps
        fi = np.floor(fp).astype(np.int64)
        within = (fp - fi) * 80.0
        k = np.clip(np.floor(within).astype(np.int64), 0, 79)
        second_half = (within - k) >= 0.5
        uniq, inv = np.unique(fi, return_inverse=True)
        bits = np.empty((len(uniq), 80), np.int64)
        ones = np.empty((len(uniq), 80), np.int64)
        for j, n in enumerate(uniq):
            bits[j], ones[j] = self._frame(int(max(n, 0)))
        # transitions since the frame start: one at every bit start, one mid-bit for each 1
        parity = (k + ones[inv, k] + (second_half & (bits[inv, k] == 1))) & 1
        amp = 10 ** (self.level_db / 20)
        out = np.where(parity == 1, -amp, amp).astype(np.float32)
        out[t < 0] = 0.0
        return out
