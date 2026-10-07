"""Comparing analysis results with the cues a programmer actually made."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge.core.compare import compare
from cueforge.core.model import BeatGrid, Cue, Project, Suggestion


def _song():
    p = Project()
    song = p.song
    song.beat_grid = BeatGrid(beats=[i * 0.5 for i in range(400)], downbeats=[i * 2.0 for i in range(100)])
    hits, main = p.lanes[0].id, p.lanes[1].id
    song.cues = [Cue(lane_id=hits, time=t) for t in (10.0, 20.0, 30.0, 40.0)] + \
                [Cue(lane_id=main, time=t) for t in (16.0, 48.0)]
    return p, song, hits, main


def test_report_counts_found_cues_and_precision():
    p, song, hits, main = _song()
    raw = [Suggestion("hit", 10.03, 0.9, ""), Suggestion("hit", 20.0, 0.7, ""), Suggestion("hit", 25.0, 0.8, ""),
           Suggestion("hit", 30.0, 0.3, ""),            # on a cue, but below the threshold
           Suggestion("section", 16.0, 0.8, ""), Suggestion("section", 33.0, 0.6, "")]
    rep = compare([(song, raw)], p.lanes, {"hit": 0.6, "section": 0.5})
    assert rep.cues == 6 and rep.found == 3 and rep.shown == 5
    kinds = {r.kind: r for r in rep.kinds}
    assert (kinds["hit"].shown, kinds["hit"].on_cues, kinds["hit"].cues_found, kinds["hit"].cues_reachable) == (3, 2, 2, 3)
    assert (kinds["section"].on_cues, kinds["section"].cues_found) == (1, 1)
    lanes = {l.lane: l for l in rep.lanes}
    assert lanes[p.lanes[0].name].cues == 4 and lanes[p.lanes[0].name].found == 2
    assert lanes[p.lanes[1].name].by_kind == {"section": 1}


def test_thresholds_learnt_from_cues():
    p, song, hits, _ = _song()
    song.cues = [Cue(lane_id=hits, time=float(t)) for t in range(10, 90, 4)]
    raw = [Suggestion("hit", float(t), 0.8, "") for t in range(10, 90, 4)]          # strong: on cues
    raw += [Suggestion("hit", t + 2.0, 0.4, "") for t in range(10, 90, 4)]          # weak: off cues
    rep = compare([(song, raw)], p.lanes, {"hit": 0.3})
    r = {k.kind: k for k in rep.kinds}["hit"]
    assert r.suggested is not None and 0.4 < r.suggested <= 0.8
