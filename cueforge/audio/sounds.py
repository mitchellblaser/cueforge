"""Synthesised click and cue-blip sounds."""
from __future__ import annotations

import numpy as np


def _tone(sr: int, freq: float, dur: float, decay: float) -> np.ndarray:
    t = np.arange(int(sr * dur)) / sr
    env = np.exp(-t / decay)
    attack = min(len(t), int(sr * 0.001))
    env[:attack] *= np.linspace(0, 1, attack)
    return (np.sin(2 * np.pi * freq * t) * env).astype(np.float32)


def click_sounds(sr: int) -> tuple[np.ndarray, np.ndarray]:
    """(accent, normal) click, stereo float32."""
    accent = _tone(sr, 1800.0, 0.05, 0.012) * 0.9
    normal = _tone(sr, 1200.0, 0.04, 0.010) * 0.7
    return np.repeat(accent[:, None], 2, 1), np.repeat(normal[:, None], 2, 1)


def blip_sound(sr: int) -> np.ndarray:
    b = _tone(sr, 2600.0, 0.03, 0.008) * 0.6 + _tone(sr, 5200.0, 0.03, 0.004) * 0.2
    return np.repeat(b[:, None], 2, 1).astype(np.float32)
