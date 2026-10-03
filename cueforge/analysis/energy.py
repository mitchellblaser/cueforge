"""Energy events: drops, breakdowns, builds, blackouts (silence) and returns."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

HOP = 512


@dataclass
class EnergyEvent:
    time: float
    confidence: float
    label: str
    reason: str


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(np.pad(x, (n // 2, n - 1 - n // 2), mode="edge"), k, mode="valid")


def detect_energy(y: np.ndarray, sr: int, downbeats: list[float] | None = None) -> list[EnergyEvent]:
    import librosa
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    db = librosa.amplitude_to_db(rms + 1e-7, ref=np.max(rms) + 1e-9)
    fps = sr / HOP
    t = librosa.frames_to_time(np.arange(len(db)), sr=sr, hop_length=HOP)
    short = _smooth(db, int(0.5 * fps))
    events: list[EnergyEvent] = []

    # --- silences / blackouts and returns -------------------------------
    silent = short < -40
    i = 0
    n = len(silent)
    while i < n:
        if silent[i]:
            j = i
            while j < n and silent[j]:
                j += 1
            dur = (j - i) / fps
            if dur >= 0.4 and i > int(1 * fps):
                events.append(EnergyEvent(float(t[i]), min(1.0, 0.6 + dur / 4), "Blackout",
                                          f"silence for {dur:.1f}s"))
                if j < n - int(1 * fps):
                    events.append(EnergyEvent(float(t[j]), min(1.0, 0.6 + dur / 4), "Return",
                                              "audio returns after silence"))
            i = j
        else:
            i += 1

    # --- drops and breakdowns: compare windows before/after -------------
    w = int(4.0 * fps)  # 4 s windows
    step = int(0.25 * fps)
    cands = []
    for k in range(w, n - w, step):
        before = np.mean(short[k - w:k])
        after = np.mean(short[k:k + w])
        cands.append((k, after - before))
    if cands:
        ks = np.array([c[0] for c in cands])
        ds = np.array([c[1] for c in cands])
        used = np.zeros(len(ds), bool)
        for idx in np.argsort(-np.abs(ds)):
            if used[idx] or abs(ds[idx]) < 5.0:
                continue
            lo, hi = max(0, idx - int(w / step)), idx + int(w / step)
            used[lo:hi] = True
            k = ks[idx]
            # locate the exact jump: steepest change of the fast envelope near k
            a, b = max(1, k - w // 2), min(n - 1, k + w // 2)
            fast = _smooth(db, max(1, int(0.05 * fps)))
            dd = np.diff(fast[a - 1:b])
            jump = a + int(np.argmax(dd) if ds[idx] > 0 else np.argmin(dd))
            tt = float(t[jump])
            mag = abs(ds[idx])
            if ds[idx] > 0:
                events.append(EnergyEvent(tt, min(1.0, 0.4 + mag / 20), "Drop / Lift",
                                          f"loudness +{mag:.0f} dB"))
            else:
                events.append(EnergyEvent(tt, min(1.0, 0.4 + mag / 20), "Breakdown",
                                          f"loudness -{mag:.0f} dB"))

    # --- builds: sustained rise in loudness + brightness ------------------
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]
    cent = _smooth(np.log(cent + 1), int(1.0 * fps))
    slow = _smooth(db, int(1.0 * fps))
    win = int(6.0 * fps)
    k = win
    while k < n - 1:
        rise_l = slow[k] - slow[k - win]
        rise_c = cent[k] - cent[k - win]
        if rise_l > 4.0 and rise_c > 0.15:
            # monotonic-ish rise
            seg = slow[k - win:k + 1]
            mono = np.mean(np.diff(seg) > -0.05)
            # a build rises gradually; a sudden step (a drop) is not a build
            sec = max(1, int(fps))
            biggest_step = max(seg[i + sec] - seg[i] for i in range(0, len(seg) - sec, max(1, sec // 4)))
            if mono > 0.7 and biggest_step < 0.45 * rise_l:
                start = float(t[k - win])
                events.append(EnergyEvent(start, min(1.0, 0.35 + rise_l / 20 + rise_c), "Build",
                                          f"rising loudness (+{rise_l:.0f} dB) and brightness"))
                k += win
                continue
        k += int(0.5 * fps)

    # snap to downbeats when close
    if downbeats:
        dbt = np.asarray(downbeats)
        for e in events:
            if e.label in ("Drop / Lift", "Breakdown", "Build"):
                j = int(np.argmin(np.abs(dbt - e.time)))
                if abs(dbt[j] - e.time) < 0.35:
                    e.time = float(dbt[j])
                    e.reason += ", on bar line"
    events.sort(key=lambda e: e.time)
    # a loudness fall right before a blackout is the same event (e.g. song end)
    blackouts = [e.time for e in events if e.label == "Blackout"]
    events = [e for e in events if not (e.label == "Breakdown"
                                       and any(0 <= b - e.time < 4.5 for b in blackouts))]
    # de-duplicate events at the same moment (keep highest confidence)
    out: list[EnergyEvent] = []
    for e in events:
        if out and abs(e.time - out[-1].time) < 0.5:
            if e.confidence > out[-1].confidence:
                out[-1] = e
        else:
            out.append(e)
    return out
