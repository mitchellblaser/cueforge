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


def _local_period(env: np.ndarray, sr: int, global_bpm: float, max_dev: float = 0.2) -> np.ndarray:
    """Per-frame beat period (in frames), tracked within +-max_dev of the global tempo
    so it can follow live tempo drift without jumping to double/half time."""
    import librosa
    from scipy.ndimage import median_filter
    fps = sr / HOP
    tg = librosa.feature.tempogram(onset_envelope=env, sr=sr, hop_length=HOP, win_length=int(8 * fps))
    lags = np.arange(tg.shape[0])
    p0 = 60.0 * fps / global_bpm
    lo, hi = int(np.floor(p0 * (1 - max_dev))), int(np.ceil(p0 * (1 + max_dev)))
    lo, hi = max(1, lo), min(tg.shape[0] - 1, hi)
    band = tg[lo:hi + 1]
    best = lags[lo:hi + 1][np.argmax(band, axis=0)].astype(float)
    # parabolic refinement of the autocorrelation peak for sub-frame precision
    idx = np.argmax(band, axis=0)
    cols = np.arange(band.shape[1])
    ok = (idx > 0) & (idx < band.shape[0] - 1)
    l, c, r = band[np.maximum(idx - 1, 0), cols], band[idx, cols], band[np.minimum(idx + 1, band.shape[0] - 1), cols]
    denom = l - 2 * c + r
    shift = np.where(ok & (np.abs(denom) > 1e-9), 0.5 * (l - r) / np.where(denom == 0, 1, denom), 0.0)
    best = best + np.clip(shift, -0.5, 0.5)
    # weak frames (silence, rubato) inherit the global period
    strength = band.max(axis=0)
    weak = strength < 0.25 * np.median(strength[strength > 0]) if np.any(strength > 0) else np.ones_like(best, bool)
    best[weak] = p0
    return median_filter(best, size=max(3, int(4 * fps)) | 1, mode="nearest")


def _dp_beats(env: np.ndarray, period: np.ndarray, tightness: float = 120.0) -> np.ndarray:
    """Dynamic-programming beat tracker (Ellis 2007) with a time-varying period."""
    n = len(env)
    o = env / (env.std() + 1e-9)
    o = o - o.mean() * 0.5
    score = np.zeros(n)
    back = -np.ones(n, dtype=np.int64)
    for t in range(n):
        P = period[t]
        a, b = int(t - 2 * P), int(t - P / 2)
        if b <= 0:
            score[t] = o[t]
            continue
        a = max(a, 0)
        taus = np.arange(a, b)
        pen = -tightness * np.log((t - taus) / P) ** 2
        cand = score[a:b] + pen
        k = int(np.argmax(cand))
        score[t] = o[t] + cand[k]
        back[t] = taus[k]
    # start from the best-scoring frame within the last period
    tail = int(max(1, period[-1]))
    t = n - tail + int(np.argmax(score[n - tail:]))
    path = [t]
    while back[t] >= 0:
        t = back[t]
        path.append(t)
    return np.asarray(path[::-1])


def _beat_features(y, sr, beats):
    """Beat-synchronous features for meter / downbeat decisions."""
    import librosa
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low = librosa.onset.onset_strength(S=S[freqs < 150] ** 0.6, sr=sr, hop_length=HOP)
    high = librosa.onset.onset_strength(S=S[freqs > 4000] ** 0.6, sr=sr, hop_length=HOP)
    full = librosa.onset.onset_strength(S=S ** 0.6, sr=sr, hop_length=HOP)
    chroma = librosa.feature.chroma_stft(S=S ** 2, sr=sr, hop_length=HOP)
    bf = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, S.shape[1] - 1)
    w = 2

    def at(x):
        return np.array([x[max(0, f - w):f + w + 1].max() for f in bf])
    # chroma averaged over each beat's span
    edges = np.r_[bf, S.shape[1]]
    C = np.stack([chroma[:, a:max(a + 1, b)].mean(axis=1) for a, b in zip(edges[:-1], edges[1:])], axis=1)
    hc = np.r_[0, np.linalg.norm(np.diff(C / (C.sum(0, keepdims=True) + 1e-9), axis=1), axis=0)]
    return at(low), at(high), at(full), C, hc


def _downbeat_phase(feats, bpb: int) -> tuple[int, float]:
    low, high, full, _, hc = feats
    n = len(low)
    scores = []
    for ph in range(bpb):
        sel = np.arange(ph, n, bpb)
        s = (low[sel].mean() / (low.mean() + 1e-9) + 0.5 * full[sel].mean() / (full.mean() + 1e-9)
             + 1.5 * hc[sel].mean() / (hc.mean() + 1e-9) + 0.3 * high[sel].mean() / (high.mean() + 1e-9))
        scores.append(s)
    order = sorted(scores, reverse=True)
    return int(np.argmax(scores)), (order[0] - order[1]) / (order[0] + 1e-9)


def _detect_meter(feats) -> int:
    """3 vs 4 beats per bar from bar-level periodicity of beat features."""
    low, high, full, C, hc = feats
    F = np.vstack([C / (np.linalg.norm(C, axis=0, keepdims=True) + 1e-9),
                   low / (low.max() + 1e-9), full / (full.max() + 1e-9)])
    F = F - F.mean(axis=1, keepdims=True)
    F /= np.linalg.norm(F, axis=0, keepdims=True) + 1e-9

    def sim(L):
        if F.shape[1] <= L + 4:
            return 0.0
        return float(np.mean(np.sum(F[:, :-L] * F[:, L:], axis=0)))
    s3 = sim(3) + sim(6) + 0.5 * sim(12)
    s4 = sim(4) + sim(8) + 0.5 * sim(16)
    # downbeat contrast also favours the true meter
    _, c3 = _downbeat_phase(feats, 3)
    _, c4 = _downbeat_phase(feats, 4)
    return 3 if (s3 - s4) + 0.5 * (c3 - c4) > 0.02 else 4


def _regularise(beats: np.ndarray, on_t: np.ndarray, win: float = 0.06) -> tuple[np.ndarray, bool]:
    """Clean up DP beats using the attacks that support them.

    * Steady material (supported beats fit one tempo): constant grid, aligned to attacks.
    * Live material: supported beats snap to their attack; runs of unsupported beats
      (pads, breakdowns, rubato) are re-spaced from the neighbouring confident tempo.
    """
    if len(on_t) == 0 or len(beats) < 8:
        return beats, False
    near = np.array([on_t[int(np.argmin(np.abs(on_t - b)))] for b in beats])
    support = np.abs(near - beats) < win
    med = float(np.median(np.diff(beats)))
    lag = float(np.median((near - beats)[support])) if support.sum() >= 4 else 0.0
    idx = np.r_[0, np.cumsum(np.maximum(1, np.round(np.diff(beats) / med)))]  # beat numbers, no drift
    if support.sum() >= 8:
        slope, icpt = np.polyfit(idx[support], near[support], 1)
        resid = near[support] - (icpt + slope * idx[support])
        # robust: a few beats pulled around by fills must not make a steady song "live"
        keep = np.abs(resid) < 0.03
        if keep.sum() >= 8:
            slope, icpt = np.polyfit(idx[support][keep], near[support][keep], 1)
            resid = near[support] - (icpt + slope * idx[support])
        mad = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))
        if mad < 0.030 and np.mean(np.abs(resid) < 0.035) > 0.85 and abs(slope - med) / med < 0.03:
            n0 = int(np.floor(icpt / slope))
            grid = np.arange(icpt - n0 * slope, beats[-1] + slope / 2, slope)
            return grid, True
    out = np.where(support, near, beats + lag).astype(float)
    # re-space unsupported runs of 3+ beats from the neighbouring confident tempo
    result: list[float] = []
    n = len(out)
    i = 0
    while i < n:
        if support[i]:
            result.append(out[i])
            i += 1
            continue
        j = i
        while j < n and not support[j]:
            j += 1
        if j - i < 3:
            result.extend(out[i:j])
        elif i > 0 and j < n:               # gap between two anchors
            local = _local_ibi(out, support, i - 1)
            k = max(1, int(round((out[j] - out[i - 1]) / local)))
            result.extend(np.linspace(out[i - 1], out[j], k + 1)[1:-1])
        elif i > 0:                         # trailing run: extrapolate
            local = _local_ibi(out, support, i - 1)
            m = int((out[j - 1] - out[i - 1]) / local + 0.5)
            result.extend(out[i - 1] + local * np.arange(1, max(m, 1) + 1))
        else:                               # leading run
            local = _local_ibi(out, support, j)
            m = int((out[j] - out[0]) / local + 0.5)
            result.extend(out[j] - local * np.arange(max(m, 1), 0, -1))
        i = j
    out = np.maximum.accumulate(np.asarray(result))
    keep = np.r_[True, np.diff(out) > 0.5 * med]
    return out[keep], False


def _local_ibi(beats: np.ndarray, support: np.ndarray, at: int, n: int = 8) -> float:
    idx = np.where(support)[0]
    if len(idx) < 2:
        return float(np.median(np.diff(beats)))
    near = idx[np.argsort(np.abs(idx - at))[:n]]
    near.sort()
    d = np.diff(beats[near]) / np.maximum(1, np.diff(near))
    return float(np.median(d)) if len(d) else float(np.median(np.diff(beats)))


def detect_beats_librosa(y: np.ndarray, sr: int, beats_per_bar: int = 0) -> BeatGrid:
    """Tempo-following beat tracker suitable for live recordings.

    beats_per_bar = 0 detects the meter (3 or 4)."""
    import librosa
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    if env.max() <= 0 or len(env) < 50:
        return BeatGrid()
    fps = sr / HOP
    global_bpm = float(librosa.feature.tempo(onset_envelope=env, sr=sr, hop_length=HOP)[0])
    period = _local_period(env, sr, global_bpm)
    frames = _dp_beats(env, period)
    beats = librosa.frames_to_time(frames, sr=sr, hop_length=HOP)
    if len(beats) < 4:
        return BeatGrid()

    onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP, units="frames")
    on_t = _attack_times(y, sr, librosa.frames_to_time(onsets, sr=sr, hop_length=HOP)) if len(onsets) else np.zeros(0)
    beats, steady = _regularise(beats, on_t)
    beats = beats[beats >= 0]
    feats = _beat_features(y, sr, beats)
    if not beats_per_bar:
        beats_per_bar = _detect_meter(feats)
    phase, clarity = _downbeat_phase(feats, beats_per_bar)
    downbeats = beats[phase::beats_per_bar]
    med = float(np.median(np.diff(beats)))
    cv = float(np.std(np.diff(beats)) / (med + 1e-9))
    stability = 1.0 - min(1.0, cv * 3)
    conf = 0.35 + 0.35 * stability + 0.3 * min(1.0, clarity * 3)
    source = "librosa (steady)" if steady else "librosa (live / tempo-following)"
    return BeatGrid([round(float(b), 6) for b in beats], [round(float(d), 6) for d in downbeats],
                    beats_per_bar, source, confirmed=False, confidence=round(conf, 3))


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


def detect_beats(y: np.ndarray, sr: int, use_deep: bool = True, beats_per_bar: int = 0) -> BeatGrid:
    if use_deep:
        g = detect_beats_beat_this(y, sr)
        if g is not None:
            return g
    return detect_beats_librosa(y, sr, beats_per_bar)
