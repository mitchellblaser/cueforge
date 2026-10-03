"""Beat grid detection: from a click track, Beat This! (optional) or librosa."""
from __future__ import annotations

import numpy as np

from ..core.model import BeatGrid

HOP = 256


def _fill_grid_gaps(beats: np.ndarray) -> np.ndarray:
    """Insert beats into gaps (e.g. a click that drops out for a bar)."""
    if len(beats) < 3:
        return beats
    period = np.median(np.diff(beats))
    out = [beats[0]]
    for b in beats[1:]:
        gap = b - out[-1]
        n = int(round(gap / period))
        if n > 1:
            step = gap / n
            for _ in range(1, n):
                out.append(out[-1] + step)
        out.append(b)
    return np.asarray(out)


def grid_from_click(y: np.ndarray, sr: int, beats_per_bar: int = 4) -> BeatGrid:
    """Build a grid from a click track: every click is a beat; accented clicks
    (louder or higher pitched) are downbeats."""
    import librosa
    pad = int(0.5 * sr)  # so a click at t=0 can still be picked as a peak
    y = np.concatenate([np.zeros(pad, y.dtype), y])
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    peaks = librosa.util.peak_pick(env, pre_max=3, post_max=3, pre_avg=10, post_avg=10,
                                   delta=env.max() * 0.15, wait=int(0.15 * sr / HOP))
    if len(peaks) < 4:
        return BeatGrid()
    # refine to sample-accurate transient start
    times = []
    win = int(0.03 * sr)
    for p in peaks:
        c = p * HOP
        a, b = max(0, c - win), min(len(y), c + win)
        seg = np.abs(y[a:b])
        if not len(seg):
            continue
        thr = seg.max() * 0.2
        first = np.argmax(seg >= thr)
        times.append((a + first - pad) / sr)
    times = _fill_grid_gaps(np.asarray(times))
    # accent features
    amps, cents = [], []
    for t in times:
        i = int(t * sr) + pad
        seg = y[i:i + int(0.03 * sr)]
        amps.append(np.abs(seg).max() if len(seg) else 0)
        if len(seg) > 64:
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            freqs = np.fft.rfftfreq(len(seg), 1 / sr)
            cents.append(float((spec * freqs).sum() / (spec.sum() + 1e-9)))
        else:
            cents.append(0.0)
    amps, cents = np.asarray(amps), np.asarray(cents)
    feat = (amps / (amps.max() + 1e-9)) + (cents / (cents.max() + 1e-9))
    # choose the phase whose clicks are most accented
    best_phase, best_score = 0, -1.0
    for ph in range(beats_per_bar):
        sel = feat[ph::beats_per_bar]
        rest = np.delete(feat, np.arange(ph, len(feat), beats_per_bar))
        score = sel.mean() - (rest.mean() if len(rest) else 0)
        if score > best_score:
            best_phase, best_score = ph, score
    accented = best_score > 0.05
    # If accent marks are irregular (not every N), trust the accents themselves
    hi = feat > (feat.min() + (feat.max() - feat.min()) * 0.5)
    if accented and hi.sum() >= 2 and np.std(feat) > 0.05:
        downbeats = times[hi]
        idx = np.where(hi)[0]
        gaps = np.diff(idx)
        if len(gaps) and np.bincount(gaps).argmax() > 1:
            beats_per_bar = int(np.bincount(gaps).argmax())
    else:
        downbeats = times[best_phase::beats_per_bar]
    ibi = np.diff(times)
    stability = 1.0 - min(1.0, float(np.std(ibi) / (np.median(ibi) + 1e-9)) * 5)
    return BeatGrid([round(float(t), 6) for t in times], [round(float(t), 6) for t in downbeats],
                    beats_per_bar, "click track", confirmed=False, confidence=round(0.7 + 0.3 * stability, 3))


def detect_beats_beat_this(y: np.ndarray, sr: int) -> BeatGrid | None:
    try:
        from beat_this.inference import Audio2Beats
    except Exception:
        return None
    try:
        a2b = Audio2Beats(checkpoint_path="final0", device="cpu", dbn=False)
        beats, downbeats = a2b(y, sr)
    except Exception:
        return None
    if len(beats) < 4:
        return None
    beats = np.asarray(beats, float)
    downbeats = np.asarray(downbeats, float)
    bpb = 4
    if len(downbeats) > 2:
        counts = [np.sum((beats >= a) & (beats < b)) for a, b in zip(downbeats[:-1], downbeats[1:])]
        bpb = int(np.bincount(counts).argmax()) or 4
    ibi = np.diff(beats)
    stability = 1.0 - min(1.0, float(np.std(ibi) / (np.median(ibi) + 1e-9)) * 3)
    return BeatGrid([round(float(b), 6) for b in beats], [round(float(d), 6) for d in downbeats],
                    bpb, "Beat This!", confirmed=False, confidence=round(0.6 + 0.35 * stability, 3))


def detect_beats_librosa(y: np.ndarray, sr: int, beats_per_bar: int = 4) -> BeatGrid:
    import librosa
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=HOP,
                                                 tightness=200, trim=False)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    if len(beats) < 4:
        return BeatGrid()
    # Regularise: fit a constant tempo where the music is steady
    ibi = np.diff(beats)
    med = np.median(ibi)
    steady = np.std(ibi) / med < 0.04
    if steady:
        idx = np.round((beats - beats[0]) / med)
        slope, icpt = np.polyfit(idx, beats, 1)
        n0 = int(np.floor(icpt / slope))
        start = icpt - n0 * slope
        beats = np.arange(start, beats[-1] + slope / 2, slope)
        beats = beats[beats >= 0]
    # Onset envelopes peak a little after the true attack: align the grid to
    # backtracked onsets (median offset of beats that have an onset nearby).
    onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP, backtrack=False,
                                        units="frames")
    if len(onsets):
        on_t = _attack_times(y, sr, librosa.frames_to_time(onsets, sr=sr, hop_length=HOP))
        diffs = []
        for b in beats:
            j = int(np.argmin(np.abs(on_t - b)))
            if abs(on_t[j] - b) < 0.08:
                diffs.append(on_t[j] - b)
        if len(diffs) >= max(4, len(beats) // 4):
            beats = beats + float(np.median(diffs))
            beats = beats[beats >= 0]
    # Downbeat phase: low-frequency onset energy is strongest on beat 1 (kick)
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low = librosa.onset.onset_strength(S=librosa.amplitude_to_db(S[freqs < 150]), sr=sr, hop_length=HOP)
    full = env
    harm_change = _chroma_change(y, sr)
    bf = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, len(low) - 1)
    scores = []
    for ph in range(beats_per_bar):
        sel = bf[ph::beats_per_bar]
        hc = (np.mean([harm_change[min(len(harm_change) - 1, f)] for f in sel]) / (harm_change.mean() + 1e-9)
              if len(harm_change) else 0)
        scores.append(low[sel].mean() / (low.mean() + 1e-9) + 0.5 * full[sel].mean() / (full.mean() + 1e-9) + hc)
    phase = int(np.argmax(scores))
    downbeats = beats[phase::beats_per_bar]
    period = np.median(np.diff(beats))
    scores_sorted = sorted(scores, reverse=True)
    clarity = (scores_sorted[0] - scores_sorted[1]) / (scores_sorted[0] + 1e-9)
    stability = 1.0 - min(1.0, float(np.std(np.diff(beats)) / (period + 1e-9)) * 3)
    conf = 0.35 + 0.35 * stability + 0.3 * min(1.0, clarity * 3)
    return BeatGrid([round(float(b), 6) for b in beats], [round(float(d), 6) for d in downbeats],
                    beats_per_bar, "librosa", confirmed=False, confidence=round(conf, 3))


def _attack_times(y: np.ndarray, sr: int, approx: np.ndarray) -> np.ndarray:
    """Refine onset times to where the waveform envelope starts rising."""
    out = []
    pre, post = int(0.06 * sr), int(0.03 * sr)
    for t in approx:
        c = int(t * sr)
        a, b = max(0, c - pre), min(len(y), c + post)
        seg = np.abs(y[a:b])
        if len(seg) < 16:
            out.append(t)
            continue
        k = max(1, int(0.002 * sr))
        env = np.convolve(seg, np.ones(k) / k, mode="same")
        base = np.percentile(env[: max(2, len(env) // 3)], 50)
        peak = env.max()
        thr = base + (peak - base) * 0.3
        idx = int(np.argmax(env >= thr))
        out.append((a + idx) / sr)
    return np.asarray(out)


def _chroma_change(y: np.ndarray, sr: int) -> np.ndarray:
    import librosa
    try:
        C = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
        d = np.r_[0, np.linalg.norm(np.diff(C, axis=1), axis=0)]
        return d / (d.max() + 1e-9)
    except Exception:
        return np.zeros(0)


def detect_beats(y: np.ndarray, sr: int, use_deep: bool = True, beats_per_bar: int = 4) -> BeatGrid:
    if use_deep:
        g = detect_beats_beat_this(y, sr)
        if g is not None:
            return g
    return detect_beats_librosa(y, sr, beats_per_bar)
