"""Chord-change detection (for colour / look changes).

Beat-synchronous chroma of the harmonic part (drums removed) plus a bass chroma,
matched against major/minor triad templates, smoothed with a Viterbi pass so a
passing note doesn't count as a chord change.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

HOP = 512
NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


@dataclass
class ChordChange:
    time: float
    chord: str
    prev: str
    confidence: float
    reason: str


def _templates() -> tuple[np.ndarray, list[str]]:
    T, names = [], []
    for root in range(12):
        for quality, ivs in (("", (0, 4, 7)), ("m", (0, 3, 7))):
            v = np.zeros(12)
            for k, i in enumerate(ivs):
                v[(root + i) % 12] = (1.0, 0.8, 0.9)[k]
            T.append(v / np.linalg.norm(v))
            names.append(NOTE_NAMES[root] + quality)
    return np.asarray(T), names


def beat_chroma(y: np.ndarray, sr: int, beats: np.ndarray, harmonic: bool = True, spectra=None) -> np.ndarray:
    import librosa
    if harmonic:
        y = spectra.harmonic_audio(2.0) if spectra is not None else librosa.effects.harmonic(y, margin=2.0)
    C = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP, fmin=librosa.note_to_hz("C2"), n_octaves=6)
    bass = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP, fmin=librosa.note_to_hz("C1"), n_octaves=2)
    bf = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, C.shape[1] - 1)
    edges = np.r_[bf, C.shape[1]]
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        b = max(a + 1, b)
        c = np.median(C[:, a:b], axis=1) + 0.5 * np.median(bass[:, a:b], axis=1)
        out.append(c)
    X = np.asarray(out).T
    return X / (np.linalg.norm(X, axis=0, keepdims=True) + 1e-9)


def detect_chord_changes(y: np.ndarray, sr: int, beats: list[float], downbeats: list[float],
                         change_penalty: float = 0.18, spectra=None) -> list[ChordChange]:
    beats = np.asarray(beats, float)
    if len(beats) < 8:
        return []
    X = beat_chroma(y, sr, beats, spectra=spectra)
    T, names = _templates()
    sim = T @ X                                   # (24, n_beats)
    n = sim.shape[1]
    # Viterbi: maximise similarity minus a penalty per change
    score = sim[:, 0].copy()
    back = np.zeros((24, n), dtype=int)
    for t in range(1, n):
        stay = score
        best_prev = int(np.argmax(score))
        switch = score[best_prev] - change_penalty
        take_switch = switch > stay
        back[:, t] = np.where(take_switch, best_prev, np.arange(24))
        score = np.where(take_switch, switch, stay) + sim[:, t]
    path = [int(np.argmax(score))]
    for t in range(n - 1, 0, -1):
        path.append(back[path[-1], t])
    path = path[::-1]
    energy = np.asarray([float(np.linalg.norm(X[:, t])) for t in range(n)])
    db = np.asarray(downbeats) if len(downbeats) else np.zeros(0)
    out = []
    for t in range(1, n):
        if path[t] == path[t - 1]:
            continue
        # how clearly the chord changed: new chord fits now, old chord fitted before
        a = float(sim[path[t], t] - sim[path[t - 1], t])
        b = float(sim[path[t - 1], t - 1] - sim[path[t], t - 1])
        clarity = np.clip((a + b) / 0.6, 0, 1)
        on_bar = bool(len(db)) and float(np.min(np.abs(db - beats[t]))) < 0.05
        conf = 0.35 + 0.45 * clarity + (0.15 if on_bar else 0.0)
        if energy[t] < 1e-3:
            continue
        out.append(ChordChange(float(beats[t]), names[path[t]], names[path[t - 1]], round(float(min(1.0, conf)), 3),
                               f"harmony {names[path[t - 1]]} → {names[path[t]]}" + (", on bar line" if on_bar else "")))
    return out
