"""Lead-line following: melody / synth / vocal phrases and their note steps.

Works on a lead stem when one is imported (or separated with Demucs), otherwise
on the harmonic part of the mix, where it tracks the most salient pitch in the
lead register and ignores pitch changes that only happen with the chords.

Gives the programmer ideas beyond drums: start a chase or a follow-spot when a
phrase starts, step a chase on each note, tilt/colour with the contour.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

HOP = 256
BINS_PER_OCT = 36
FMIN_NOTE = "C3"
N_OCT = 5


@dataclass
class Phrase:
    start: float
    end: float
    notes: list[float] = field(default_factory=list)
    pitches: list[float] = field(default_factory=list)   # MIDI
    confidence: float = 0.0
    label: str = ""
    reason: str = ""
    idea: str = ""


def _salience(y: np.ndarray, sr: int):
    import librosa
    fmin = librosa.note_to_hz(FMIN_NOTE)
    C = np.abs(librosa.cqt(y, sr=sr, hop_length=HOP, fmin=fmin, n_bins=BINS_PER_OCT * N_OCT,
                           bins_per_octave=BINS_PER_OCT))
    C = C ** 0.5
    sal = np.zeros_like(C)
    for h, w in ((1, 1.0), (2, 0.6), (3, 0.4), (4, 0.25)):
        shift = int(round(BINS_PER_OCT * np.log2(h)))
        if shift < C.shape[0]:
            sal[:C.shape[0] - shift] += w * C[shift:]
    midi = librosa.hz_to_midi(fmin) + np.arange(C.shape[0]) * 12 / BINS_PER_OCT
    return sal, midi, C


def foreground(y: np.ndarray, sr: int) -> np.ndarray:
    """Remove drums (HPSS) and the repeating accompaniment (REPET-SIM: each frame is
    compared with similar frames elsewhere in the song; what repeats is background).
    What is left is mostly the lead line / vocal."""
    import librosa
    hop = 1024  # coarse frames keep the all-pairs similarity affordable on long songs
    D = librosa.stft(y, n_fft=2048, hop_length=hop)
    H, _ = librosa.decompose.hpss(D, margin=(3.0, 1.0), kernel_size=17)
    S = np.abs(H)
    width = int(librosa.time_to_frames(2.0, sr=sr, hop_length=hop))
    bg = librosa.decompose.nn_filter(S, aggregate=np.median, metric="cosine", width=width)
    bg = np.minimum(S, bg)
    mask = librosa.util.softmask(S - bg, 2.0 * bg, power=2)
    fg = librosa.istft(H * mask, hop_length=hop, length=len(y))
    rest = librosa.istft(H * (1 - mask), hop_length=hop, length=len(y))
    return fg, rest


def track_lead(y: np.ndarray, sr: int, harmonic: bool = True, lead_low_midi: float = 60.0):
    """Return (times, midi pitch or nan, voicing strength) per frame."""
    import librosa
    from scipy.ndimage import median_filter
    rest = None
    if harmonic:
        y, rest = foreground(y, sr)
    sal, midi, C = _salience(y, sr)
    reg = midi >= lead_low_midi
    s = sal[reg]
    m = midi[reg]
    top = np.argmax(s, axis=0)
    peak = s[top, np.arange(s.shape[1])]
    floor = np.median(s, axis=0) + 1e-9
    strength = peak / floor
    level = peak / (np.percentile(peak, 95) + 1e-9)
    voiced = (strength > 2.5) & (level > 0.12)
    if harmonic:
        # On the mix, decide "lead playing" from foreground energy (the residue after
        # removing drums and repeating accompaniment), split into on/off by Otsu.
        # foreground-to-background ratio in the lead register: independent of how
        # loud the band is in each section
        _, _, Cb = _salience(rest, sr)
        fe = C[reg].sum(axis=0)
        be = Cb[reg].sum(axis=0)
        k = max(1, int(0.08 * sr / HOP))
        ratio = np.convolve(np.log(fe + 1e-9) - np.log(be + 1e-9), np.ones(k) / k, mode="same")
        audible = np.log(fe + be + 1e-9) > np.log(np.percentile(fe + be, 95) + 1e-9) - 5
        if audible.sum() > 20:
            thr, sep = _otsu(ratio[audible])
            if sep > 0.8:
                voiced &= (ratio > thr) & audible
    pitch = np.where(voiced, m[top], np.nan)
    # smooth octave/neighbour jitter
    filled = np.where(np.isnan(pitch), 0, pitch)
    sm = median_filter(filled, size=5)
    pitch = np.where(voiced, sm, np.nan)
    times = librosa.frames_to_time(np.arange(len(pitch)), sr=sr, hop_length=HOP)
    return times, pitch, np.clip((strength - 2.5) / 4, 0, 1) * np.clip(level * 2, 0, 1), y


def polyphony(y: np.ndarray, sr: int) -> float:
    """Typical number of simultaneous pitches (1 = a single line, 3+ = chords)."""
    sal, midi, C = _salience(y, sr)
    energy = C.sum(axis=0)
    frames = np.where(energy > np.percentile(energy, 60))[0]
    if len(frames) == 0:
        return 0.0
    frames = frames[:: max(1, len(frames) // 400)]
    counts = []
    semis = BINS_PER_OCT // 12
    nb = C.shape[0]
    for f in frames:
        col = C[:, f].copy()
        first = None
        n = 0
        for _ in range(6):
            k = int(np.argmax(sal[:, f] if n == 0 else col))
            v = col[k]
            if first is None:
                first = v
            elif v < 0.4 * first:
                break
            n += 1
            # remove this pitch and its harmonic series
            for h in range(1, 9):
                c = k + int(round(BINS_PER_OCT * np.log2(h)))
                if c >= nb:
                    break
                col[max(0, c - semis):min(nb, c + semis + 1)] = 0
        counts.append(n)
    return float(np.median(counts))


LEAD_WORDS = ("vocal", "vox", "voice", "lead", "melody", "sax", "solo", "trumpet", "flute", "singer")


def is_lead_like(y: np.ndarray, sr: int, name: str = "") -> bool:
    if any(w in name.lower() for w in LEAD_WORDS):
        return True
    return polyphony(y, sr) <= 2.0


def _otsu(x: np.ndarray) -> tuple[float, float]:
    """Two-class threshold; also returns the separation of the class means."""
    hist, edges = np.histogram(x, bins=64)
    centers = (edges[:-1] + edges[1:]) / 2
    w = hist.astype(float)
    best, best_t, sep = -1.0, float(np.median(x)), 0.0
    for i in range(1, len(w)):
        w0, w1 = w[:i].sum(), w[i:].sum()
        if w0 == 0 or w1 == 0:
            continue
        m0 = (w[:i] * centers[:i]).sum() / w0
        m1 = (w[i:] * centers[i:]).sum() / w1
        between = w0 * w1 * (m0 - m1) ** 2
        if between > best:
            best, best_t, sep = between, float(edges[i]), float(m1 - m0)
    return best_t, sep


def _notes(times, pitch, y_h, sr, min_dur=0.07):
    """Segment the pitch track into notes: a new note starts on a pitch change of a
    semitone or more that holds, or on a re-attack (harmonic onset) at the same pitch."""
    import librosa
    env = librosa.onset.onset_strength(y=y_h, sr=sr, hop_length=HOP, fmin=200, n_mels=96)
    onsets = set(librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP, units="frames").tolist())
    hold = max(1, int(min_dur * sr / HOP))
    notes = []  # (start_frame, end_frame, midi)
    cur = None
    for i in range(len(pitch)):
        p = pitch[i]
        if np.isnan(p):
            if cur is not None and i - cur[0] >= hold:
                notes.append((cur[0], i, cur[1]))
            cur = None
            continue
        if cur is None:
            cur = [i, p]
            continue
        stable_change = abs(p - cur[1]) >= 0.8 and np.all(
            np.abs(np.nan_to_num(pitch[i:i + hold], nan=-99) - p) < 0.8)
        reattack = i in onsets and i - cur[0] >= hold and abs(p - cur[1]) < 0.8
        if stable_change or reattack:
            if i - cur[0] >= hold:
                notes.append((cur[0], i, cur[1]))
            cur = [i, p]
    if cur is not None and len(pitch) - cur[0] >= hold:
        notes.append((cur[0], len(pitch), cur[1]))
    return [(float(times[a]), float(times[min(b, len(times) - 1)]), float(m)) for a, b, m in notes]


def _onset_notes(times, pitch, y_fg, sr):
    """Notes from onsets of the foreground signal (robust when the pitch track
    flickers between lead and accompaniment): one note per onset in a voiced region."""
    import librosa
    env = librosa.onset.onset_strength(y=y_fg, sr=sr, hop_length=HOP, fmin=200, n_mels=96)
    fr = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP, units="frames",
                                    delta=0.15, wait=int(0.08 * sr / HOP))
    voiced = ~np.isnan(pitch)
    notes = []
    for k, f in enumerate(fr):
        if f >= len(pitch):
            continue
        w = voiced[f:f + 6]
        if w.mean() < 0.5:
            continue
        end = fr[k + 1] if k + 1 < len(fr) else len(pitch) - 1
        seg = np.where(voiced[f:end])[0]
        stop = f + (seg[-1] + 1 if len(seg) else 1)
        p = float(np.nanmedian(pitch[f:stop])) if np.any(voiced[f:stop]) else float("nan")
        notes.append((float(times[f]), float(times[min(stop, len(times) - 1)]), p))
    return notes


def detect_phrases(y: np.ndarray, sr: int, chord_times: list[float] | None = None, harmonic: bool = True,
                   gap: float = 0.6, source: str = "") -> list[Phrase]:
    times, pitch, strength, y_h = track_lead(y, sr, harmonic)
    notes = _notes(times, pitch, y_h, sr)
    if chord_times and harmonic:
        # Long notes that start with a chord change and don't move until the next
        # chord are accompaniment (pads / comping), not a lead line.
        ct = np.asarray(chord_times)
        notes = [n for n in notes
                 if not (len(ct) and np.min(np.abs(ct - n[0])) < 0.08 and (n[1] - n[0]) > 0.6)]
    phrases: list[Phrase] = []
    cur: list[tuple[float, float, float]] = []
    for n in notes:
        if cur and n[0] - cur[-1][1] > gap:
            phrases.append(_make_phrase(cur, times, strength, source))
            cur = []
        cur.append(n)
    if cur:
        phrases.append(_make_phrase(cur, times, strength, source))
    return [p for p in phrases if len(p.notes) >= 2 and p.end - p.start >= 0.25]


def _make_phrase(notes, times, strength, source) -> Phrase:
    starts = [n[0] for n in notes]
    pitches = [n[2] for n in notes]
    a, b = notes[0][0], notes[-1][1]
    fa, fb = np.searchsorted(times, a), np.searchsorted(times, b)
    st = float(np.mean(strength[fa:max(fa + 1, fb)]))
    rate = len(notes) / max(0.25, b - a)
    slope = np.polyfit(np.arange(len(pitches)), pitches, 1)[0] if len(pitches) > 2 else 0.0
    if rate > 5:
        shape = "fast run / arpeggio"
        idea = "fast chase or pixel run following the notes"
    elif slope > 0.6:
        shape = "rising line"
        idea = "tilt up / build intensity with the line"
    elif slope < -0.6:
        shape = "falling line"
        idea = "tilt down / fade with the line"
    else:
        shape = "melody"
        idea = "follow-spot or feature look; step a chase on each note"
    who = f"{source} " if source else "Lead "
    conf = min(1.0, 0.35 + 0.4 * st + 0.05 * min(len(notes), 6))
    return Phrase(a, b, starts, pitches, round(conf, 3), f"{who}phrase: {shape}".strip(),
                  f"{len(notes)} notes over {b - a:.1f}s, pitch {min(pitches):.0f}–{max(pitches):.0f}", idea)
