"""Synthetic test song with known beats, hits and sections."""
from __future__ import annotations

import numpy as np

SR = 44100


def _kick(sr):
    t = np.arange(int(sr * 0.25)) / sr
    f = 50 + 100 * np.exp(-t / 0.03)
    return np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t / 0.12)


def _snare(sr, rng):
    t = np.arange(int(sr * 0.18)) / sr
    return (rng.standard_normal(len(t)) * 0.6 + np.sin(2 * np.pi * 190 * t) * 0.4) * np.exp(-t / 0.05)


def _hat(sr, rng):
    t = np.arange(int(sr * 0.05)) / sr
    noise = rng.standard_normal(len(t))
    noise = np.diff(noise, prepend=0)  # crude high-pass
    return noise * np.exp(-t / 0.01) * 0.15


def _pad(sr, dur, freqs):
    t = np.arange(int(sr * dur)) / sr
    y = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    env = np.minimum(1, t / 0.05) * np.minimum(1, (dur - t) / 0.05)
    return y * env


def make_song(bpm: float = 120.0, sr: int = SR, seed: int = 0):
    """Return (stereo audio, info) for a 4/4 song with sections:

    intro 8 bars (pad only, quiet) | verse 8 bars (drums+pad) | chorus 8 bars (loud, new chords)
    | breakdown 4 bars (pad only) | drop 8 bars (loud)
    """
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    bar = beat * 4
    sections = [("intro", 8, 0.25, False, [220, 277, 330]),
                ("verse", 8, 0.5, True, [196, 247, 294]),
                ("chorus", 8, 1.0, True, [262, 330, 392, 523]),
                ("breakdown", 4, 0.2, False, [175, 220, 262]),
                ("drop", 8, 1.0, True, [262, 330, 392, 523])]
    total_bars = sum(s[1] for s in sections)
    dur = total_bars * bar + 1.0
    y = np.zeros(int(dur * sr))
    kick, snare, hat = _kick(sr), _snare(sr, rng), _hat(sr, rng)

    def add(sig, t, g=1.0):
        i = int(round(t * sr))
        n = min(len(sig), len(y) - i)
        y[i:i + n] += sig[:n] * g

    boundaries, beats, downbeats, kicks, snares = [], [], [], [], []
    t = 0.0
    for name, nbars, level, drums, chord in sections:
        boundaries.append((t, name))
        for b in range(nbars):
            # chord changes each 2 bars
            add(_pad(sr, bar * 2, [f * (1.0 if (b // 2) % 2 == 0 else 1.122) for f in chord]), t, 0.25 * level) if b % 2 == 0 else None
            for k in range(4):
                bt = t + k * beat
                beats.append(bt)
                if k == 0:
                    downbeats.append(bt)
                if drums:
                    if k in (0, 2):
                        add(kick, bt, 0.9 * level)
                        kicks.append(bt)
                    if k in (1, 3):
                        add(snare, bt, 0.5 * level)
                        snares.append(bt)
                    add(hat, bt + beat / 2, level)
            t += bar
    y /= np.abs(y).max() * 1.1
    stereo = np.stack([y, y], axis=1).astype(np.float32)
    info = dict(bpm=bpm, beats=beats, downbeats=downbeats, boundaries=boundaries,
                kicks=kicks, snares=snares, duration=dur)
    return stereo, info


def make_click(info: dict, sr: int = SR, offset: float = 0.0):
    dur = info["duration"]
    y = np.zeros(int(dur * sr))
    t = np.arange(int(sr * 0.03)) / sr
    dset = set(round(d, 6) for d in info["downbeats"])
    for b in info["beats"]:
        f = 1600 if round(b, 6) in dset else 1000
        g = 1.0 if round(b, 6) in dset else 0.6
        s = np.sin(2 * np.pi * f * t) * np.exp(-t / 0.008) * g
        i = int(round((b + offset) * sr))
        n = min(len(s), len(y) - i)
        if n > 0:
            y[i:i + n] += s[:n]
    return np.stack([y, y], 1).astype(np.float32)


if __name__ == "__main__":
    import soundfile as sf
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "test_song.wav"
    audio, info = make_song()
    sf.write(out, audio, SR)
    sf.write(out.replace(".wav", "_click.wav"), make_click(info), SR)
    print("wrote", out, info["duration"])
