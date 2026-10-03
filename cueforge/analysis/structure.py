"""Song structure: section boundaries (verse/chorus/drop...) as cue-change suggestions.

Baseline: beat-synchronous chroma + MFCC + loudness, self-similarity novelty
(checkerboard kernel), peaks snapped to bar lines, segments clustered into
repeating labels (A, B, C...).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

HOP = 512


@dataclass
class Boundary:
    time: float
    confidence: float
    label: str
    reason: str


def _checkerboard(n: int) -> np.ndarray:
    sign = np.ones((2 * n, 2 * n))
    sign[:n, n:] = -1
    sign[n:, :n] = -1
    g = np.exp(-0.5 * ((np.arange(2 * n) - n + 0.5) / (n / 2)) ** 2)
    return sign * np.outer(g, g)


def detect_sections(y: np.ndarray, sr: int, beats: list[float], downbeats: list[float],
                    min_section: float = 7.0) -> list[Boundary]:
    import librosa
    duration = len(y) / sr
    if duration < 10:
        return []
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=HOP)
    rms = librosa.feature.rms(y=y, hop_length=HOP)
    rms_db = librosa.amplitude_to_db(rms + 1e-6)

    # Sync to beats when we have them, else ~0.5 s frames
    if beats and len(beats) > 8:
        frames = librosa.time_to_frames(np.asarray(beats), sr=sr, hop_length=HOP)
        frames = np.unique(np.clip(frames, 0, chroma.shape[1] - 1))
    else:
        frames = np.arange(0, chroma.shape[1], max(1, int(0.5 * sr / HOP)))
    C = librosa.util.sync(chroma, frames, aggregate=np.median)
    M = librosa.util.sync(mfcc, frames, aggregate=np.mean)
    L = librosa.util.sync(rms_db, frames, aggregate=np.mean)
    times = librosa.frames_to_time(np.r_[0, frames], sr=sr, hop_length=HOP)[: C.shape[1]]

    def z(x):
        return (x - x.mean(axis=1, keepdims=True)) / (x.std(axis=1, keepdims=True) + 1e-6)

    F = np.vstack([z(C) * 1.0, z(M) * 0.7, z(L) * 1.2])
    F = F / (np.linalg.norm(F, axis=0, keepdims=True) + 1e-9)
    SSM = F.T @ F
    n = SSM.shape[0]
    period = np.median(np.diff(times)) if len(times) > 1 else 0.5
    k = max(4, int(round(8.0 / period)))  # ~8 s half-kernel (about 4 bars)
    k = min(k, max(2, n // 4))
    K = _checkerboard(k)
    pad = np.pad(SSM, k, mode="edge")
    nov = np.array([np.sum(pad[i:i + 2 * k, i:i + 2 * k] * K) for i in range(n)])
    nov = np.maximum(nov, 0)
    if nov.max() <= 0:
        return []
    nov /= nov.max()

    min_gap = max(2, int(round(min_section / period)))
    peaks = librosa.util.peak_pick(nov, pre_max=min_gap // 2, post_max=min_gap // 2,
                                   pre_avg=min_gap, post_avg=min_gap, delta=0.05, wait=min_gap)
    cand = [(float(times[p]), float(nov[p])) for p in peaks if 2.0 < times[p] < duration - 2.0]

    # Snap to nearest downbeat
    db = np.asarray(downbeats) if downbeats else None
    snapped = []
    for t, s in cand:
        reason = "change in harmony/timbre/loudness"
        if db is not None and len(db):
            j = int(np.argmin(np.abs(db - t)))
            if abs(db[j] - t) < max(1.0, period * 3):
                t = float(db[j])
                reason += ", on bar line"
        snapped.append((t, s, reason))

    # Label segments by clustering their mean features
    bounds = [0.0] + [t for t, _, _ in snapped] + [duration]
    seg_feats = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        sel = (times >= a) & (times < b)
        seg_feats.append(F[:, sel].mean(axis=1) if sel.any() else np.zeros(F.shape[0]))
    labels = _label_segments(np.asarray(seg_feats))
    seg_loud = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        sel = (times >= a) & (times < b)
        seg_loud.append(float(L[0, sel].mean()) if sel.any() else -80.0)
    loudest = max(seg_loud) if seg_loud else 0

    out = []
    for i, (t, s, reason) in enumerate(snapped):
        seg_idx = i + 1
        lab = labels[seg_idx]
        prev_lab = labels[seg_idx - 1]
        name = f"Section {lab}"
        if seg_loud[seg_idx] >= loudest - 2.0:
            name += " (high energy)"
        if labels.count(lab) > 1:
            reason += f", repeats elsewhere"
        conf = 0.35 + 0.55 * s + (0.1 if lab != prev_lab else 0.0)
        out.append(Boundary(t, round(min(1.0, conf), 3), name, reason))
    return out


def _label_segments(feats: np.ndarray, threshold: float = 0.35) -> list[str]:
    labels: list[str] = []
    protos: list[np.ndarray] = []
    for f in feats:
        nf = f / (np.linalg.norm(f) + 1e-9)
        best, best_d = -1, 1e9
        for j, p in enumerate(protos):
            d = 1 - float(nf @ p)
            if d < best_d:
                best, best_d = j, d
        if best >= 0 and best_d < threshold:
            labels.append(chr(ord("A") + best))
        else:
            protos.append(nf)
            labels.append(chr(ord("A") + min(len(protos) - 1, 25)))
    return labels


def detect_sections_allin1(path: str) -> list[Boundary] | None:
    """Optional: All-In-One music structure analyser (if installed)."""
    try:
        import allin1
    except Exception:
        return None
    try:
        res = allin1.analyze(path)
    except Exception:
        return None
    out = []
    for seg in res.segments[1:]:
        out.append(Boundary(float(seg.start), 0.8, seg.label.capitalize(), f"All-In-One: {seg.label}"))
    return out
