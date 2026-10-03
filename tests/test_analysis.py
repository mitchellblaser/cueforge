"""Analysis accuracy on a synthetic song with known ground truth."""
import os
import sys

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge.analysis.pipeline import AnalysisOptions, run_analysis
from cueforge.audio.loader import load_audio
from cueforge.core import editing
from cueforge.core.model import Project, Track
from tests.synth import SR, make_click, make_song


@pytest.fixture(scope="module")
def song(tmp_path_factory):
    d = tmp_path_factory.mktemp("song")
    audio, info = make_song()
    path = d / "song.wav"
    sf.write(path, audio, SR)
    cpath = d / "click.wav"
    sf.write(cpath, make_click(info), SR)
    return str(path), str(cpath), info


@pytest.fixture(scope="module")
def analysed(song):
    path, _, info = song
    p = Project()
    t = Track("song", path)
    p.tracks.append(t)
    res = run_analysis(p, {t.id: load_audio(path)}, AnalysisOptions(use_deep_models=False))
    return p, res, info


def test_grid(analysed):
    _, res, info = analysed
    g = res.grid
    assert g.bpm() == pytest.approx(120, abs=0.5)
    assert not g.confirmed
    gb = np.asarray(g.beats)
    err = [np.min(np.abs(gb - b)) for b in info["beats"]]
    assert np.mean(err) < 0.02
    dbt = np.asarray(g.downbeats)
    hit = [np.min(np.abs(dbt - d)) < 0.03 for d in info["downbeats"]]
    assert np.mean(hit) > 0.9


def test_sections_found(analysed):
    _, res, info = analysed
    cues = [s for s in res.suggestions if s.kind in ("section", "energy")]
    times = np.asarray([s.time for s in cues])
    for t, name in info["boundaries"][1:]:
        assert np.min(np.abs(times - t)) < 0.1, name


def test_hits_ranked(analysed):
    p, res, info = analysed
    hits = [s for s in res.suggestions if s.kind == "hit"]
    true = np.asarray(info["kicks"] + info["snares"])
    th = p.analysis.thresholds["hit"]
    shown = [h for h in hits if h.confidence >= th]
    correct = [h for h in shown if np.min(np.abs(true - h.time)) < 0.04]
    assert len(correct) / len(shown) > 0.9          # precision at default threshold
    assert len(correct) / len(true) > 0.9           # recall


def test_click_track_grid(song):
    path, cpath, info = song
    p = Project()
    t = Track("song", path)
    c = Track("click", cpath, role="Click")
    p.tracks += [t, c]
    res = run_analysis(p, {t.id: load_audio(path), c.id: load_audio(cpath)},
                       AnalysisOptions(hits=False, sections=False, energy=False, use_deep_models=False))
    g = res.grid
    assert g.source == "click track"
    assert len(g.beats) == len(info["beats"])
    assert np.max(np.abs(np.asarray(g.beats) - info["beats"])) < 0.002
    assert np.allclose(g.downbeats, info["downbeats"], atol=0.002)


def test_suggestions_never_touch_cues(analysed):
    p, res, _ = analysed
    q = Project()
    lane = q.lanes[0].id
    c = editing.add_cue(q, lane, 16.0, label="mine")
    editing.merge_suggestions(q, res.suggestions, res.kinds)
    assert len(q.cues) == 1 and q.cues[0] is c and c.label == "mine"
    assert all(s.status == "pending" for s in q.suggestions)
