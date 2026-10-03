"""Drum fill detection: fast runs into the next bar -> strobe suggestions.

What a programmer puts a strobe on is the *fast* stuff: 16ths / sextuplets / 32nd
rolls on snare and toms that drive into a downbeat. So we count drum strokes per
half-beat, compare with what the drummer normally plays at that point in the bar
(the same half-beat in the surrounding bars — so a busy groove such as 16th-note
hi-hats doesn't count), and keep fast runs that end on a bar line.

Slow fills (quarter / 8th-note tom hits) are deliberately not suggested.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FAST_HOP = 128               # fine resolution: 32nd notes at 160 BPM are ~47 ms apart
FILL_BAND = (70, 800)        # snare body + toms; hats/cymbals sit above this
MAX_BARS = 2                 # longer runs are reported as snare-roll builds (up to 8 bars)


@dataclass
class Fill:
    start: float
    end: float          # the downbeat it lands on
    beats: float
    confidence: float
    label: str
    reason: str
    rate: float = 0.0   # strokes per beat


def _band_env(y: np.ndarray, sr: int, percussive: bool, spectra=None):
    """Onset envelope + energy of the snare-body / tom band (hats barely reach it)."""
    import librosa
    S = None
    if spectra is not None:
        S = spectra.percussive(2.0) if percussive else spectra.S
        hop = spectra_hop = 256
    if S is None:
        hop = FAST_HOP
        S = np.abs(librosa.stft(y, n_fft=1024, hop_length=hop))
        if percussive:
            _, S = librosa.decompose.hpss(S, margin=(1.0, 2.0), kernel_size=17)
    n_fft = (S.shape[0] - 1) * 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    sel = (freqs >= FILL_BAND[0]) & (freqs < FILL_BAND[1])
    env = librosa.onset.onset_strength(S=S[sel] ** 0.6, sr=sr, hop_length=hop, lag=1, max_size=1)
    return env, S[sel].sum(axis=0), hop


def _fast_periodicity(env: np.ndarray, end: float, beat_frames: float) -> tuple[float, float]:
    """How strongly the envelope repeats at 3, 4, 6 or 8 strokes per beat in the ~1.25
    beats leading up to `end` (looking back, so a fill is measured up to the bar line and
    not smeared into the landing). Returns (score, strokes-per-beat of the best lag)."""
    w = int(beat_frames * 1.25)
    seg = env[max(0, int(end - w)):int(end)]
    if len(seg) < 8:
        return 0.0, 0.0
    seg = seg - seg.mean()
    if seg.std() == 0:
        return 0.0, 0.0
    ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
    ac = ac / ac[0]
    best, rate = -1.0, 0.0
    for r in (3, 4, 6, 8):
        lag = int(round(beat_frames / r))
        if 1 <= lag < len(ac) and ac[lag] > best:
            best, rate = float(ac[lag]), float(r)
    return best, rate


def detect_fills(y: np.ndarray, sr: int, beats: list[float], downbeats: list[float], bpb: int,
                 percussive: bool = True, spectra=None, clean_source: bool = False, **_ignored) -> list[Fill]:
    import librosa
    beats = np.asarray(beats, float)
    if len(beats) < bpb * 3 or len(downbeats) < 3:
        return []
    env, level, hop = _band_env(y, sr, percussive, None)   # fine hop: 32nds need resolution
    fps = sr / hop
    beat_len = float(np.median(np.diff(beats)))
    beat_frames = beat_len * fps
    db = np.asarray(downbeats, float)
    first_db = int(np.argmin(np.abs(beats - db[0])))
    edges = np.sort(np.r_[beats, (beats[:-1] + beats[1:]) / 2])   # half-beat units
    n = len(edges) - 1
    upb = 2 * bpb
    pos = (np.arange(n) - 2 * first_db) % upb

    fast = np.zeros(n)
    rate = np.zeros(n)
    energy = np.zeros(n)
    for u in range(n):
        fast[u], rate[u] = _fast_periodicity(env, edges[u + 1] * fps, beat_frames)
        a, b = int(edges[u] * fps), max(int(edges[u] * fps) + 1, int(edges[u + 1] * fps))
        energy[u] = float(level[a:b].mean()) if a < len(level) else 0.0
    song_level = float(np.percentile(energy, 90)) + 1e-9

    # contrast with the same half-beat in the surrounding bars (the groove)
    score = np.zeros(n)
    for u in range(n):
        nb = [u + k * upb for k in (-2, -1, 1, 2) if 0 <= u + k * upb < n]
        if not nb:
            continue
        acf_c = fast[u] - float(np.median(fast[nb]))
        e_c = np.log2((energy[u] + 1e-9) / (float(np.median(energy[nb])) + 1e-9))
        score[u] = acf_c + 0.08 * float(np.clip(e_c, -2, 2))
        if energy[u] < 0.08 * song_level:     # nothing playing
            score[u] = 0.0
    pos_scores = score[score > 0]
    thr = max(0.1, float(np.percentile(pos_scores, 85))) if len(pos_scores) else 0.1
    if clean_source:   # an isolated drum stem: fills stand out clearly, don't over-tighten
        thr = min(thr, 0.18)

    # onsets in the band, to start the strobe on the first stroke of the run
    on = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=hop, units="time",
                                    wait=max(1, int(0.03 * fps)), delta=0.05)

    # a fill must finish in the last half-beat before a bar line
    land_units = {upb - 1}

    fills: list[Fill] = []
    u = n - 1
    while u >= 0:
        if pos[u] not in land_units or score[u] < thr or fast[u] < 0.08:
            u -= 1
            continue
        j = u
        while j - 1 >= 0 and score[j - 1] >= 0.6 * thr and fast[j - 1] >= 0.06 and (u - (j - 1)) < 8 * upb:
            j -= 1
        end = float(edges[u + 1]) if u + 1 <= n else float(edges[-1])
        r = float(np.median(rate[j:u + 1]))
        # Start = first stroke of the evenly spaced fast run that leads into the bar line:
        # walk back through the band onsets while the spacing stays at the fill's rate.
        ioi = beat_len / max(r, 3.0)
        before = [t for t in on if t < end - 0.02]
        start = float(edges[j])
        if before:
            k = len(before) - 1
            start = before[k]
            while k > 0 and before[k] - before[k - 1] <= 1.6 * ioi and end - before[k - 1] <= MAX_BARS * bpb * beat_len:
                k -= 1
                start = before[k]
            # never reach more than a beat before the region that sounded fast
            start = float(max(start, edges[j] - 0.5 * beat_len))
            if start > edges[j]:
                start = float(min(start, edges[j] + 0.5 * beat_len))
        length_beats = (end - start) / beat_len
        strength = float(np.mean(score[j:u + 1]) / thr)
        lf = int(end * fps)
        landing = float(env[lf:lf + int(0.05 * fps) + 1].max() / (np.percentile(env, 99) + 1e-9)) \
            if lf < len(env) else 0.0
        rel_level = float(np.clip(np.mean(energy[j:u + 1]) / song_level, 0, 1))
        conf = 0.4 + 0.12 * min(2.5, strength) + 0.1 * min(1.0, landing) + 0.08 * min(1.0, length_beats)
        conf *= 0.75 + 0.25 * rel_level
        speed = {3: "triplets", 4: "16ths", 6: "sextuplets", 8: "32nd roll"}.get(int(r), "fast")
        if length_beats > MAX_BARS * bpb:
            label = f"Snare roll build ({length_beats / bpb:.0f} bars)"
        else:
            bt = f"{length_beats:.1f}".rstrip("0").rstrip(".")
            label = f"Drum fill: {speed} ({bt} beat{'s' if length_beats > 1.05 else ''})"
        fills.append(Fill(start, end, round(length_beats, 2), round(float(min(1.0, conf)), 3), label,
                          f"fast {speed} on snare/toms into the downbeat", r))
        u = j - 1
    fills.reverse()
    return fills
