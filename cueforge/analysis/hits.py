"""Hit (accent) detection.

Hits are onsets that stand out from their surroundings — what a programmer would
put a bump, flash or strobe on — not every hi-hat. Confidence combines global
strength (vs. the whole song) and local contrast (vs. the surrounding seconds).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

HOP = 256

# (name, low Hz, high Hz)
BANDS = {
    "kick": (30, 150),
    "snare": (150, 2500),
    "full": (30, 8000),
}


@dataclass
class Hit:
    time: float
    confidence: float
    reason: str
    label: str = ""


def _band_env(S: np.ndarray, freqs: np.ndarray, lo: float, hi: float, sr: int) -> np.ndarray:
    import librosa
    sel = (freqs >= lo) & (freqs < hi)
    if not sel.any():
        return np.zeros(S.shape[1])
    # Compressed magnitude (not dB): loud, weighty attacks score higher than quiet
    # high-frequency ticks, which is closer to what reads as a "hit" for lighting.
    return librosa.onset.onset_strength(S=S[sel] ** 0.6, sr=sr, hop_length=HOP, lag=1, max_size=3)


def _score_peaks(env: np.ndarray, sr: int, min_gap: float) -> list[tuple[int, float]]:
    import librosa
    if env.max() <= 0:
        return []
    norm = env / (np.percentile(env, 99.5) + 1e-9)
    peaks = librosa.util.peak_pick(norm, pre_max=4, post_max=4, pre_avg=20, post_avg=20,
                                   delta=0.08, wait=max(1, int(min_gap * sr / HOP)))
    if not len(peaks):
        return []
    # local context: median of +-2 s
    w = int(2.0 * sr / HOP)
    out = []
    for p in peaks:
        a, b = max(0, p - w), min(len(norm), p + w)
        local = np.median(norm[a:b]) + 1e-6
        local_hi = np.percentile(norm[a:b], 90) + 1e-6
        strength = min(1.0, norm[p])
        contrast = min(1.0, (norm[p] - local) / (local_hi * 1.5))
        conf = 0.55 * strength + 0.45 * max(0.0, contrast)
        out.append((int(p), float(conf)))
    return out


def detect_hits(y: np.ndarray, sr: int, bands=("kick", "snare", "full"), min_gap: float = 0.09,
                source: str = "") -> list[Hit]:
    import librosa
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    found: list[Hit] = []
    for band in bands:
        lo, hi = BANDS[band]
        env = _band_env(S, freqs, lo, hi, sr)
        for p, conf in _score_peaks(env, sr, min_gap):
            t = float(librosa.frames_to_time(p, sr=sr, hop_length=HOP))
            name = {"kick": "Kick", "snare": "Snare", "full": "Accent"}[band]
            label = f"{source} {name}".strip() if source else name
            found.append(Hit(t, conf, f"{label.lower()} onset", label))
    return _merge(found, min_gap)


def _merge(hits: list[Hit], window: float) -> list[Hit]:
    """Merge hits from several bands that fall on the same moment."""
    hits.sort(key=lambda h: h.time)
    merged: list[Hit] = []
    for h in hits:
        if merged and h.time - merged[-1].time < window:
            m = merged[-1]
            if h.confidence > m.confidence:
                h.reason = f"{h.reason} + {m.reason}"
                h.confidence = min(1.0, h.confidence + 0.1 * m.confidence)
                merged[-1] = h
            else:
                m.reason = f"{m.reason} + {h.reason}"
                m.confidence = min(1.0, m.confidence + 0.1 * h.confidence)
        else:
            merged.append(h)
    return merged
