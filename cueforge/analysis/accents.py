"""Accents: the hits a lighting programmer would actually put a cue on.

A steady groove has a loud kick and snare on every beat, and lighting every one of them is
noise. What stands out to a listener, and gets a bump, flash or blinder, is what *breaks*
the pattern:

* a crash or a hit where the groove doesn't normally have one (section starts, fill landings),
* a band stab: drums and band together on an unusual beat,
* a stop: everyone hits, then silence.

Method: the beat grid is cut into 16th-note slots and each bar is compared, slot by slot,
with the bars around it (the pattern before *and* after; a groove that just starts or ends
isn't an accent). What's louder than both is a candidate. Stops are found from the drop in
level right after a hit. Finally the best few per stretch of bars are kept, so a song gets a
handful of strong suggestions instead of hundreds.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .hits import BANDS, HOP, _band_env

SUB = 4                  # slots per beat (16ths)
CONTEXT = 4              # bars either side the pattern is learnt from
MAX_PER_4_BARS = 3       # density cap


@dataclass
class Accent:
    time: float
    confidence: float
    label: str
    reason: str
    idea: str


IDEAS = {
    "Stop": "hit then blackout / freeze for the stop",
    "Crash": "big hit: full-rig flash or blinder",
    "Stab": "flash / bump with the band stab",
    "Accent": "bump / flash on the accent",
}


def _slot_times(beats: np.ndarray) -> np.ndarray:
    """16th-note slot times between the beats (the last beat interval repeated at the end)."""
    if len(beats) < 2:
        return np.zeros(0)
    b = np.r_[beats, beats[-1] + (beats[-1] - beats[-2])]
    idx = np.arange((len(b) - 1) * SUB) / SUB
    return np.interp(idx, np.arange(len(b)), b)


def _sample(env: np.ndarray, frames: np.ndarray, half: int) -> np.ndarray:
    """Envelope maximum around each slot (live timing isn't exactly on the grid). Onset
    envelopes peak ~40 ms after the attack itself, so the window reaches further forward."""
    n = len(env)
    out = np.zeros(len(frames))
    for i, f in enumerate(frames):
        a, b = max(0, f - half), min(n, f + 3 * half + 1)
        out[i] = env[a:b].max() if b > a else 0.0
    return out


def _pattern_excess(v: np.ndarray, bar_of: np.ndarray, pos_of: np.ndarray) -> np.ndarray:
    """How much each slot is louder than the same slot in the surrounding bars: the larger of
    the medians before and after (so a groove starting or stopping isn't an accent)."""
    nb = int(bar_of.max()) + 1 if len(bar_of) else 0
    npos = int(pos_of.max()) + 1 if len(pos_of) else 0
    grid = np.full((nb, npos), np.nan)
    grid[bar_of, pos_of] = v
    out = np.zeros(len(v))
    for i in range(len(v)):
        b, p = bar_of[i], pos_of[i]
        before = grid[max(0, b - CONTEXT):b, p]
        after = grid[b + 1:b + 1 + CONTEXT, p]
        before, after = before[~np.isnan(before)], after[~np.isnan(after)]
        ref = [np.median(x) for x in (before, after) if len(x)]
        expected = max(ref) if ref else 0.0
        out[i] = v[i] - expected
    return out


def _ring(S_high: np.ndarray, frames: np.ndarray, sr: int, half: int = 3) -> np.ndarray:
    """How much high-band energy keeps ringing 80-300 ms after each slot's attack, relative to
    the attack: a crash rings on, hi-hats and ride strokes die away between strokes."""
    e = S_high.sum(axis=0)
    n = len(e)
    lo, hi = int(0.08 * sr / HOP), int(0.3 * sr / HOP)
    out = np.zeros(len(frames))
    peak = np.percentile(e, 99.5) + 1e-9
    for i, f in enumerate(frames):
        a0 = max(0, f - half)
        win = e[a0:f + 2 * half + 1]
        if not len(win):
            continue
        p = a0 + int(np.argmax(win))            # the attack near the slot (live timing)
        if p + hi >= n:
            continue
        a = e[p]
        sustain = e[p + lo:p + hi].min()
        out[i] = min(1.0, sustain / (a + 1e-9) / 0.3) * min(1.0, a / peak * 2)
    return out


def _cymbal_attack(e: np.ndarray, t: float, before: float, sr: int) -> float | None:
    """Time the high band starts rising into its peak, searched from `before` s ahead of t."""
    import librosa
    f0 = int(librosa.time_to_frames(max(0.0, t - before), sr=sr, hop_length=HOP))
    f1 = int(librosa.time_to_frames(t + 0.04, sr=sr, hop_length=HOP))
    seg = e[f0:f1 + 1]
    if len(seg) < 3:
        return None
    p = int(np.argmax(seg))
    base = seg[:max(1, p)].min()
    thr = base + 0.5 * (seg[p] - base)
    k = p
    while k > 0 and seg[k - 1] >= thr:
        k -= 1
    return float(librosa.frames_to_time(f0 + k, sr=sr, hop_length=HOP))


def detect_accents(y: np.ndarray, sr: int, beats: list[float], downbeats: list[float],
                   drums: np.ndarray | None = None, spectra=None, percussive: bool = True,
                   max_per_4_bars: int = MAX_PER_4_BARS,
                   exclude: list[tuple[float, float]] = ()) -> list[Accent]:
    """`y` is the full mix (band stabs, stops); `drums` an isolated drum source if there is
    one (otherwise the mix's percussive part is used for the drum bands). `exclude`: time
    ranges (drum fills) that have their own suggestion; their landing still counts."""
    import librosa
    beats_a = np.asarray(beats, float)
    if len(beats_a) < 8 or not len(downbeats):
        return []
    slots = _slot_times(beats_a)
    dur = len(y) / sr
    keep = slots < dur - 0.05
    slots = slots[keep]
    beat_idx = (np.arange(len(slots)) // SUB)
    sub_idx = np.arange(len(slots)) % SUB
    db_idx = np.searchsorted(beats_a, np.asarray(downbeats) - 1e-3)
    bar_of = np.searchsorted(db_idx, beat_idx, side="right") - 1
    first = bar_of >= 0                                   # slots before bar 1 are left out
    beat_in_bar = beat_idx - db_idx[np.clip(bar_of, 0, None)]
    pos_of = beat_in_bar * SUB + sub_idx
    slots, bar_of, pos_of = slots[first], bar_of[first], pos_of[first]
    if not len(slots):
        return []

    # band envelopes: drums from the drum source (or the percussive part of the mix)
    if drums is not None:
        Sd = np.abs(librosa.stft(drums, n_fft=2048, hop_length=HOP))
    elif spectra is not None and percussive:
        Sd = spectra.percussive(2.0)
    else:
        Sd = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
        if percussive:
            _, Sd = librosa.decompose.hpss(Sd, margin=(1.0, 2.0), kernel_size=17)
    S_full = spectra.S if spectra is not None else np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    frames = librosa.time_to_frames(slots, sr=sr, hop_length=HOP)
    half = max(1, int(0.035 * sr / HOP))

    def norm(e):
        return e / (np.percentile(e, 99.5) + 1e-9)

    envs = {}
    for band in ("kick", "snare", "crash"):
        lo, hi = BANDS[band]
        envs[band] = norm(_band_env(Sd, freqs, lo, hi, sr))
    envs["band"] = norm(_band_env(S_full, freqs, 30, 8000, sr))      # the whole band together
    weights = {"kick": 0.6, "snare": 0.6, "crash": 0.5, "band": 1.0, "ring": 2.2}
    excess = {}
    level = {}
    for k, env in envs.items():
        v = _sample(env, frames, half)
        level[k] = v
        excess[k] = np.clip(_pattern_excess(v, bar_of, pos_of), 0, None)
    hi_sel = (freqs >= 5000) & (freqs < 11000)
    e_high = Sd[hi_sel].sum(axis=0)
    # ringing counts only where a cymbal starts (not in the tail of the previous one)
    ring = _ring(Sd[hi_sel], frames, sr, half) * np.clip(level["crash"] / 0.3, 0, 1)
    c = level["crash"]
    peak_here = c >= np.maximum(np.r_[0.0, c[:-1]], np.r_[c[1:], 0.0])
    rising = c - np.r_[0.0, c[:-1]] >= 0.05
    ring[~(peak_here | rising)] = 0.0
    level["ring"] = ring
    excess["ring"] = np.clip(_pattern_excess(ring, bar_of, pos_of), 0, None)
    # the band's excess counts fully only with the drums (a stab), not for melody notes alone
    drums_hit = np.maximum.reduce([excess[k] for k in ("kick", "snare", "crash", "ring")]) > 0.2
    excess["band"] = excess["band"] * np.where(drums_hit, 1.0, 0.3)
    score = sum(weights[k] * np.minimum(excess[k], 1.0) for k in weights) / 2.0
    unison = sum((excess[k] > 0.25).astype(int) for k in ("kick", "snare", "crash", "band"))
    score = score * (1.0 + 0.25 * np.clip(unison - 1, 0, None))

    # stops: a strong hit followed by a big drop in level for the next beat or more
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    rdb = librosa.amplitude_to_db(rms + 1e-6)
    ibi = float(np.median(np.diff(beats_a)))
    stop = np.zeros(len(slots))
    quiet = float(np.median(rdb)) - 4.0

    def fr(t):
        return int(librosa.time_to_frames(t, sr=sr, hop_length=HOP))
    for i, t in enumerate(slots):
        f = frames[i]
        a, b = fr(t + max(ibi, 0.35)), fr(t + max(2.5 * ibi, 1.2))
        pre = fr(max(0.0, t - max(2 * ibi, 1.0)))
        if b >= len(rdb) or f - pre < 2:
            continue
        before = float(np.percentile(rdb[pre:f], 75))
        after = float(np.percentile(rdb[a:b], 75))
        drop = before - after
        if drop > 9 and after < quiet and level["band"][i] > 0.35:
            stop[i] = min(1.0, (drop - 9) / 10 + 0.5)
    score = np.maximum(score, stop * 1.1)

    # loudness of the surroundings: accents in quiet passages matter less for lighting
    from scipy.ndimage import uniform_filter1d
    loud_db = librosa.amplitude_to_db(uniform_filter1d(rms, int(2 * sr / HOP)) + 1e-6)
    loud = np.clip((loud_db - (loud_db.max() - 20)) / 20, 0, 1)
    score = score * (0.6 + 0.4 * loud[np.clip(frames, 0, len(loud) - 1)])

    # where in the bar: accents land on the one, a beat or an 8th far more often than on a 16th
    metric = np.where(pos_of == 0, 1.2, np.where(sub_idx[first] == 0, 1.0,
                                                np.where(sub_idx[first] == 2, 0.9, 0.7)))
    score = score * metric
    # inside a fill the fill's own suggestion covers it (its landing is just after the fill)
    for a, b in exclude:
        score[(slots >= a - 0.03) & (slots < b - 0.04)] = 0.0
    # local maxima only (one accent per hit, not its neighbouring 16ths)
    cand = [i for i in range(len(score)) if score[i] > 0.12 and
            score[i] >= score[max(0, i - 2):i + 3].max()]
    cand.sort(key=lambda i: -score[i])
    chosen: list[int] = []
    for i in cand:
        bar4 = bar_of[i] // 4
        if sum(1 for j in chosen if bar_of[j] // 4 == bar4) >= max_per_4_bars:
            continue
        if any(abs(slots[j] - slots[i]) < 0.6 * ibi for j in chosen):
            continue
        chosen.append(i)
    chosen.sort()

    from .beats import _attack_times
    src = drums if drums is not None else y
    out: list[Accent] = []
    for i in chosen:
        s = float(score[i])
        if stop[i] * 1.1 >= s - 1e-9 and stop[i] > 0:
            label, why = "Stop", "the band hits and stops"
        elif excess["ring"][i] > 0.25:
            label, why = "Crash", "crash where the groove has none"
        elif unison[i] >= 2 and excess["band"][i] > 0.2:
            label, why = "Stab", "drums and band hit together off the pattern"
        else:
            label, why = "Accent", "louder than the pattern around it"
        on_one = pos_of[i] == 0
        conf = float(np.clip(0.35 + 0.55 * s, 0.0, 1.0))
        reason = f"{why} (bar {int(bar_of[i]) + 1}{', on the one' if on_one else ''})"
        t = float(slots[i])
        # a crash's ring can show a 16th late (a roll or fill masks its attack): place it
        # on the slot nearest to where the cymbal actually starts
        sb = sub_idx[first]
        if label == "Crash" and i > 0 and sb[i] % 2 == 1 and sb[i - 1] % 2 == 0:
            att = _cymbal_attack(e_high, t, slots[i] - slots[i - 1] + 0.03, sr)
            if att is not None:
                j = i - 1 if abs(slots[i - 1] - att) < abs(slots[i] - att) else i
                if j != i:
                    t = float(slots[j])
                    on_one = pos_of[j] == 0
                    reason = f"{why} (bar {int(bar_of[j]) + 1}{', on the one' if on_one else ''})"
        r = _attack_times(src, sr, np.asarray([t]))[0]
        if abs(r - t) < 0.03:
            t = float(r)
        out.append(Accent(round(t, 6), round(conf, 3), label, reason, IDEAS[label]))
    return out
