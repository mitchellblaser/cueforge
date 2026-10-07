"""Accuracy regression tests on the multi-genre corpus (live + studio styles).

Rendered with FluidSynth + a GM soundfont; skipped where those aren't installed.
Set CUEFORGE_SKIP_SLOW=1 to skip.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import corpus

pytestmark = [pytest.mark.skipif(not corpus.available(), reason="FluidSynth / GM soundfont not installed"),
              pytest.mark.skipif(os.environ.get("CUEFORGE_SKIP_SLOW") == "1", reason="slow tests skipped")]


@pytest.fixture(scope="module")
def mix_scores():
    from tests import evaluate as E
    return {spec.name: E.score(*E.analyse_song(spec)[:2]) for spec in corpus.corpus()}


@pytest.fixture(scope="module")
def stem_scores():
    from tests import evaluate as E
    return {spec.name: E.score(*E.analyse_song(spec, use_stems=True)[:2]) for spec in corpus.corpus()}


def mean(scores, key):
    return float(np.mean([s[key] for s in scores.values()]))


def test_beats_follow_live_tempo(mix_scores):
    assert mean(mix_scores, "beatAMLt") >= 0.88
    for name in ("rock_live", "funk_live", "edm_club"):
        assert mix_scores[name]["beatF"] >= 0.9, name


def test_downbeats_and_meter(mix_scores):
    assert mean(mix_scores, "downF") >= 0.8
    assert mix_scores["waltz_34"]["meter"] == 3
    assert mix_scores["rock_live"]["meter"] == 4


def test_fast_fills(mix_scores):
    """Fast fills (16ths / sextuplets / 32nd rolls into a bar line); slow 8th-note tom
    fills are deliberately not suggested."""
    assert mean(mix_scores, "fillF") >= 0.65
    assert mean(mix_scores, "fillP") >= 0.75
    for name in ("edm_club", "funk_live", "waltz_34", "pop_halftime"):
        assert mix_scores[name]["fillF"] >= 0.75, name


def test_hits_are_accents_not_every_drum_hit(mix_scores, stem_scores):
    """Hits suggest the accents a programmer lights (section-start crashes, band stabs,
    stops), a handful per minute, instead of every kick and snare (~100+ per minute)."""
    assert mean(mix_scores, "accR") >= 0.75
    assert mean(stem_scores, "accR") >= 0.65         # the ballad's stems give a wrong (3/4) grid
    for scores in (mix_scores, stem_scores):
        assert mean(scores, "accP") >= 0.3
        assert mean(scores, "hitsPerMin") <= 15
        for name, s in scores.items():
            assert s["hitsPerMin"] <= 30, name


def test_chord_changes(mix_scores):
    assert mean(mix_scores, "chordF") >= 0.85


def test_section_changes(mix_scores):
    assert mean(mix_scores, "sectionR") >= 0.75


def test_lead_lines_from_stems(stem_scores):
    assert mean(stem_scores, "phraseF") >= 0.65
