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
    "crash": (5000, 11000),   # only kept when the cymbal rings on (see _is_crash)
    "full": (30, 8000),       # generic accents (used for non-drum stems)
}
DEFAULT_BANDS = ("kick", "snare", "crash")


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


def _is_crash(S_high: np.ndarray, frame: int, sr: int) -> float:
    """Sustain ratio of high-band energy ~250 ms after the onset (crash/china ring on,
    hi-hats and ride ticks die away). Returns 0..1."""
    n = S_high.shape[1]
    e = S_high.sum(axis=0)
    a = e[frame:min(n, frame + 3)].max() if frame < n else 0
    # minimum over 80-300 ms: a crash rings on throughout, while hats/ride dip
    # between strokes (a later stroke must not look like sustain)
    lo, hi = frame + int(0.08 * sr / HOP), frame + int(0.3 * sr / HOP)
    if hi >= n:
        return 0.0
    b = float(e[lo:hi].min())
    return float(min(1.0, b / (a + 1e-9) / 0.3))


def detect_hits(y: np.ndarray, sr: int, bands=DEFAULT_BANDS, min_gap: float = 0.09,
                source: str = "", percussive: bool = True, spectra=None) -> list[Hit]:
    """percussive=True separates the drums from tonal instruments first (HPSS), so
    piano/guitar/synth note starts are not mistaken for hits."""
    import librosa
    if spectra is not None:
        S = spectra.percussive(2.0) if percussive else spectra.S
    else:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
        if percussive:
            _, S = librosa.decompose.hpss(S, margin=(1.0, 2.0), kernel_size=17)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    # loudness of the surrounding ~2 s relative to the loudest part of the song:
    # hits in quiet passages matter less for lighting than hits in loud ones
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    from scipy.ndimage import uniform_filter1d
    loud_db = librosa.amplitude_to_db(uniform_filter1d(rms, int(2 * sr / HOP)) + 1e-6)
    loud = np.clip((loud_db - (loud_db.max() - 18)) / 18, 0, 1)
    found: list[Hit] = []
    for band in bands:
        lo, hi = BANDS[band]
        env = _band_env(S, freqs, lo, hi, sr)
        high = S[(freqs >= lo) & (freqs < hi)] if band == "crash" else None
        for p, conf in _score_peaks(env, sr, 0.25 if band == "crash" else min_gap):
            if band == "crash":
                sustain = _is_crash(high, p, sr)
                if sustain < 0.6:
                    continue
                conf = conf * sustain
            conf = conf * (0.55 + 0.45 * float(loud[min(p, len(loud) - 1)]))
            t = float(librosa.frames_to_time(p, sr=sr, hop_length=HOP))
            name = {"kick": "Kick", "snare": "Snare", "crash": "Crash", "full": "Accent"}[band]
            label = f"{source} {name}".strip() if source else name
            found.append(Hit(t, conf, f"{label.lower()} onset", label))
    # onset envelopes peak after the attack begins: move each hit to its attack start
    if found:
        from .beats import _attack_times
        refined = _attack_times(y, sr, np.asarray([h.time for h in found]))
        for h, r in zip(found, refined):
            if -0.05 <= r - h.time <= 0.01:
                h.time = float(r)
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
