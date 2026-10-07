"""Sections from a spoken cue track: timing, grouping repeated calls, and leaving guide
vocals alone. The keyword model is tested only where it's installed (with espeak-ng)."""
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge.analysis import cuetrack as C

SR = 22050
BAR, BEAT = 2.0, 0.5


def _word(f1, f2, dur, f0=120.0, seed=0):
    """A vowel-like sound gliding between two formant pairs (stands in for a spoken word)."""
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    a = np.linspace(0, 1, len(t))
    for k in range(1, 40):
        f = k * f0
        F1 = f1[0] + (f1[1] - f1[0]) * a
        F2 = f2[0] + (f2[1] - f2[0]) * a
        g = np.exp(-((f - F1) / 120) ** 2) + 0.7 * np.exp(-((f - F2) / 160) ** 2)
        out += g * np.sin(2 * np.pi * f * t)
    env = np.minimum(1, np.minimum(t / 0.02, (dur - t) / 0.05))
    return (out * env / (np.abs(out).max() + 1e-9) * 0.5).astype(np.float32)


WORDS = {"A": _word((700, 300), (1200, 2300), 0.45), "B": _word((300, 750), (2300, 1000), 0.5),
         "C": _word((500, 500), (1800, 900), 0.4), "3": _word((350, 350), (2400, 2400), 0.22, 150),
         "4": _word((650, 450), (1000, 800), 0.22, 150)}


def _cue_track(plan, bars=40, noise=0.002):
    """plan: [(section start bar, word)]; each call is said on beat 1 of the bar before,
    followed by a "3, 4" count on beats 3 and 4."""
    y = np.random.default_rng(1).normal(0, noise, int((bars * BAR + 2) * SR)).astype(np.float32)
    for bar, w in plan:
        t = 1.0 + (bar - 1) * BAR
        for word, at in ((w, t), ("3", t + 2 * BEAT), ("4", t + 3 * BEAT)):
            x = WORDS[word]
            i = int(at * SR)
            y[i:i + len(x)] += x
    downs = [1.0 + k * BAR for k in range(bars)]
    beats = [1.0 + k * BEAT for k in range(bars * 4)]
    return y, downs, beats


def test_calls_place_sections_on_the_next_downbeat_and_group_repeats():
    plan = [(4, "A"), (12, "B"), (20, "A"), (28, "C"), (32, "B")]
    y, downs, beats = _cue_track(plan)
    out = C.detect_spoken_sections(y, SR, downs, beats, use_spotter=False)
    assert [round(s.time, 3) for s in out] == [1.0 + b * BAR for b, _ in plan]
    labels = [s.label for s in out]
    assert labels[0] == labels[2] and labels[1] == labels[4]          # repeats share a name
    assert len({labels[0], labels[1], labels[3]}) == 3                # different calls don't
    assert all(s.confidence >= 0.75 and "cue track" in s.reason for s in out)


def test_guide_vocal_track_is_not_read_as_cues():
    y, downs, beats = _cue_track([(4, "A"), (12, "B")])
    sing = np.tile(WORDS["A"], int(len(y) / len(WORDS["A"])) + 1)[:len(y)]
    assert C.detect_spoken_sections(y + sing, SR, downs, beats, use_spotter=False) == []


def test_without_a_grid_sections_follow_the_call():
    y, _, _ = _cue_track([(4, "A"), (12, "B")])
    out = C.detect_spoken_sections(y, SR, [], [], use_spotter=False)
    assert len(out) == 2 and all(0 < s.time - (1.0 + (b - 1) * BAR) < 2.5 for s, b in zip(out, (4, 12)))


def test_pipeline_uses_the_cue_track_for_sections():
    from cueforge.analysis.pipeline import AnalysisOptions, run_analysis
    from cueforge.core.model import Project, Track
    from tests.evaluate import audio_data
    from tests.synth import SR as SSR, make_song
    song, info = make_song()
    bar = 2.0
    secs = [t for t, _ in info["boundaries"][1:]]
    yc = np.zeros(len(song), np.float32)
    import librosa
    for k, t in enumerate(secs):
        x = librosa.resample(WORDS["AB"[k % 2]], orig_sr=SR, target_sr=SSR)
        i = int((t - bar) * SSR)
        yc[i:i + len(x)] += x
    p = Project()
    t = Track("song", "song.wav")
    g = Track("cues", "cues.wav", role="Cue/Guide")
    p.tracks += [t, g]
    audio = {t.id: audio_data(song, SSR), g.id: audio_data(yc, SSR)}
    res = run_analysis(p, audio, AnalysisOptions(use_deep_models=False, hits=False, fills=False,
                                                 harmony=False, melody=False, energy=False))
    spoken = [s for s in res.suggestions if s.kind == "section" and "next downbeat" in s.reason]
    assert len(spoken) == len(secs)
    for s, t0 in zip(spoken, secs):
        assert abs(s.time - t0) < 0.05
    others = [s for s in res.suggestions if s.kind == "section" and "next downbeat" not in s.reason]
    assert all(min(abs(o.time - t0) for t0 in secs) > 1.0 * bar for o in others)


@pytest.mark.skipif(not (C.spotter_available() and shutil.which("espeak-ng")),
                    reason="keyword model or espeak-ng not installed")
def test_spotter_names_spoken_words(tmp_path):
    import io
    import soundfile as sf
    import librosa

    def say(text):
        wav = subprocess.run(["espeak-ng", "-s", "150", "--stdout", text], capture_output=True).stdout
        x, s = sf.read(io.BytesIO(wav))
        return librosa.resample(x.astype(np.float32), orig_sr=s, target_sr=SR)
    y = np.random.default_rng(0).normal(0, 0.003, int(60 * SR)).astype(np.float32)
    for at, w in ((5.0, "Verse"), (20.0, "Bridge"), (35.0, "Verse")):
        x = say(w)
        y[int(at * SR):int(at * SR) + len(x)] += 0.6 * x
    downs = [1.0 + k * BAR for k in range(29)]
    out = C.detect_spoken_sections(y, SR, downs, [1.0 + k * BEAT for k in range(116)])
    assert [s.label for s in out][:2] == ["Verse", "Bridge"] and out[2].label == "Verse"
