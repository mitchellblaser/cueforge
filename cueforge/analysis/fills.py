"""Drum fill (and snare-roll build) detection.

A fill is where the drummer leaves the groove for a few beats — usually busier,
tom/snare heavy, hi-hats stop — and lands on the next downbeat (often with a
crash). We learn the groove from the preceding bars, score how much each beat
departs from it, and keep runs of departing beats that end at a bar line.

For lighting: suggest a strobe (or chase) across the fill and a hit on the landing.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

HOP = 256
SLOTS = 4  # sub-divisions per beat (16ths in 4/4)
BANDS = [(30, 120), (70, 400), (400, 2500), (5000, 11000)]  # kick, toms/body, snare attack, hats/cymbals


@dataclass
class Fill:
    start: float
    end: float          # the downbeat it lands on
    beats: int
    confidence: float
    label: str
    reason: str


def _env_bands(y: np.ndarray, sr: int, percussive: bool, spectra=None):
    import librosa
    if spectra is not None:
        S = spectra.percussive(2.0) if percussive else spectra.S
    else:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
        if percussive:
            _, S = librosa.decompose.hpss(S, margin=(1.0, 2.0), kernel_size=17)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    envs = []
    energy = []
    for lo, hi in BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        envs.append(librosa.onset.onset_strength(S=S[sel] ** 0.6, sr=sr, hop_length=HOP, lag=1, max_size=1))
        energy.append(S[sel].sum(axis=0))
    return np.asarray(envs), np.asarray(energy)


def _slot_features(envs: np.ndarray, energy: np.ndarray, beats: np.ndarray, sr: int) -> np.ndarray:
    """(n_beats, n_bands * SLOTS + n_bands) features: onset peaks per sub-division, plus
    per-band energy share (toms raise the low-mid share)."""
    fps = sr / HOP
    n = len(beats) - 1
    nb = envs.shape[0]
    F = np.zeros((n, nb * SLOTS + nb))
    norm = np.percentile(envs, 99, axis=1)[:, None] + 1e-9
    E = envs / norm
    en = energy / (energy.sum(axis=0, keepdims=True) + 1e-9)
    for i in range(n):
        a, b = beats[i] * fps, beats[i + 1] * fps
        edges = np.linspace(a, b, SLOTS + 1)
        for s in range(SLOTS):
            lo = int(edges[s] - 0.25 * (edges[1] - edges[0]))
            hi = int(edges[s + 1] - 0.25 * (edges[1] - edges[0]))
            lo, hi = max(lo, 0), max(min(hi, E.shape[1]), max(lo, 0) + 1)
            F[i, s::SLOTS][:nb] = E[:, lo:hi].max(axis=1) if hi <= E.shape[1] else 0
        fa, fb = int(a), max(int(a) + 1, int(b))
        F[i, nb * SLOTS:] = en[:, fa:fb].mean(axis=1) if fb <= en.shape[1] else 0
    return F


def detect_fills(y: np.ndarray, sr: int, beats: list[float], downbeats: list[float], bpb: int,
                 percussive: bool = True, history_bars: int = 4, spectra=None) -> list[Fill]:
    beats = np.asarray(beats, float)
    if len(beats) < bpb * (history_bars + 2) or len(downbeats) < 3:
        return []
    envs, energy = _env_bands(y, sr, percussive, spectra)
    F = _slot_features(envs, energy, beats, sr)
    n = len(F)
    db = np.asarray(downbeats)
    # position of each beat within its bar (0 = downbeat)
    first_db = int(np.argmin(np.abs(beats - db[0])))
    pos = (np.arange(n) - first_db) % bpb
    nb = len(BANDS)
    act = F[:, :nb * SLOTS].reshape(n, nb, SLOTS)

    score = np.zeros(n)
    fillish = np.zeros(n, bool)
    detail = [""] * n
    for i in range(n):
        same = [j for j in range(i - bpb * history_bars, i) if j >= 0 and pos[j] == pos[i]]
        if len(same) < 2:
            continue
        if np.median(act[same, :3].max(axis=(1, 2))) < 0.15:
            continue                   # no groove yet (drums just entering)
        tmpl = np.median(F[same], axis=0)
        spread = np.median(np.abs(F[same] - tmpl), axis=0) + 0.05
        dev = np.abs(F[i] - tmpl) / spread
        groove_break = float(np.mean(np.sort(dev)[-6:]))      # strongest departures
        t_act = np.median(act[same], axis=0)
        busy = float(np.sum(act[i, 1:3] > 0.3) - np.sum(t_act[1:3] > 0.3))  # toms/snare slots
        tom_shift = float(F[i, nb * SLOTS + 1] - tmpl[nb * SLOTS + 1])         # low-mid energy share
        hats_drop = float(np.sum(t_act[3] > 0.25) - np.sum(act[i, 3] > 0.25))
        s = 0.15 * groove_break + 0.35 * max(0.0, busy) + 6.0 * max(0.0, tom_shift) + 0.15 * max(0.0, hats_drop)
        loudness = float(act[i, :3].max())
        if loudness < 0.15:            # silence / no drums is not a fill
            s = 0.0
        score[i] = s
        fillish[i] = busy >= 1 or tom_shift > 0.03
        detail[i] = f"busy {busy:+.0f}, toms {tom_shift:+.2f}, hats {-hats_drop:+.0f}"

    # Contrast against the same beat in the surrounding bars (before AND after): a
    # fill stands out from its neighbours, while drums entering or a new groove
    # keep scoring high for several bars and cancel out.
    raw = score.copy()
    for i in range(n):
        neigh = [raw[i + k * bpb] for k in (-2, -1, 1, 2) if 0 <= i + k * bpb < n]
        if neigh:
            score[i] = max(0.0, raw[i] - float(np.median(neigh)))
    thr = max(1.0, float(np.percentile(score[score > 0], 75)) if np.any(score > 0) else 1.0)
    fills: list[Fill] = []
    i = n - 1
    while i >= 0:
        # a fill must finish on the last beat(s) before a downbeat
        if pos[(i + 1) % n] != 0 if i + 1 < n else True:
            i -= 1
            continue
        if score[i] < thr:
            i -= 1
            continue
        j = i
        while (j - 1 >= 0 and score[j - 1] >= thr * 0.85 and fillish[j - 1]
               and i - (j - 1) < 2 * bpb and pos[j - 1] != bpb - 1):
            j -= 1
        length = i - j + 1
        # A fill is transient: if the bar after the landing keeps playing the same
        # pattern, this is a new groove (section change), not a fill.
        after = [k + bpb for k in range(j, i + 1) if k + bpb < n]
        if after:
            d_after = float(np.mean(np.abs(F[j:j + len(after)] - F[after])))
            hist = [k - bpb for k in range(j, i + 1) if k - bpb >= 0]
            d_before = float(np.mean(np.abs(F[j:j + len(hist)] - F[hist]))) if hist else 1.0
            if d_after < 0.5 * d_before:
                i = j - 1
                continue
        land = beats[i + 1] if i + 1 < len(beats) else beats[i] + (beats[i] - beats[i - 1])
        mean_s = float(score[j:i + 1].mean())
        # landing accent (crash / big kick on the downbeat after) raises confidence
        landing = float(act[i + 1, [0, 3], 0].max()) if i + 1 < n else 0.0
        conf = min(1.0, 0.3 + 0.12 * mean_s / thr + 0.25 * min(1.0, landing) + 0.05 * min(length, 4))
        if length > 2 * bpb:
            label = f"Snare roll / build ({length // bpb} bars)"
        else:
            label = f"Drum fill ({length} beat{'s' if length > 1 else ''})"
        fills.append(Fill(float(beats[j]), float(land), length, round(conf, 3), label,
                          f"groove break into the downbeat ({detail[i]})"))
        i = j - 1
    fills.reverse()
    return fills
