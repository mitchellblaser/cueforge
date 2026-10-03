import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge.core import editing
from cueforge.core.model import BeatGrid, Project, Suggestion, Track
from cueforge.core.project_io import load_project, save_project
from cueforge.core.timecode import (FRAME_RATES, frame_count_to_tc, parse_tc, seconds_to_tc,
                                    tc_to_frame_count)
from cueforge.core.undo import UndoStack


@pytest.mark.parametrize("key", list(FRAME_RATES))
def test_timecode_roundtrip(key):
    rate = FRAME_RATES[key]
    for frames in [0, 1, 29, 30, 1799, 1800, 1801, 17982, 17983, 107892, 215784, 3600 * 30]:
        h, m, s, f = frame_count_to_tc(frames, rate)
        assert tc_to_frame_count(h, m, s, f, rate) == frames


def test_drop_frame_labels():
    rate = FRAME_RATES["29.97df"]
    # frame 1800 is the first frame of minute 1, labelled 00:01:00;02
    assert frame_count_to_tc(1800, rate) == (0, 1, 0, 2)
    # minute 10 does not drop
    assert frame_count_to_tc(17982, rate) == (0, 10, 0, 0)


def test_seconds_tc_with_offset():
    rate = FRAME_RATES["25"]
    assert seconds_to_tc(1.0, rate, 3600) == "01:00:01:00"
    assert parse_tc("01:00:01:12", rate, 3600) == pytest.approx(1.48)
    assert parse_tc("2.5", rate) == 2.5
    with pytest.raises(ValueError):
        parse_tc("00:00:00:30", rate)


def test_beatgrid_from_tempo():
    g = BeatGrid.from_tempo(120, 1.0, 10.0, 4)
    assert g.beats[0] == pytest.approx(0.0)
    assert 1.0 in g.downbeats and 3.0 in g.downbeats
    assert g.bpm() == pytest.approx(120)
    assert g.nearest_beat(1.2) == pytest.approx(1.0)
    assert g.step(1.0, 1) == pytest.approx(1.5)
    assert g.step(1.0, -1) == pytest.approx(0.5)


def test_project_roundtrip(tmp_path):
    p = Project()
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    p.tracks.append(Track("song", str(audio)))
    c = editing.add_cue(p, p.lanes[0].id, 1.234)
    c.label = "Chorus"
    p.suggestions.append(Suggestion("hit", 2.0, 0.8, "kick onset"))
    p.beat_grid = BeatGrid.from_tempo(128, 0.5, 30)
    path = tmp_path / "proj.cueproj"
    save_project(p, str(path))
    q = load_project(str(path))
    assert q.tracks[0].path == str(audio)
    assert q.cues[0].label == "Chorus"
    assert q.cues[0].time == pytest.approx(c.time)
    assert q.suggestions[0].reason == "kick onset"
    assert q.beat_grid.bpm() == pytest.approx(128, abs=0.01)
    assert q.analysis.lane_for_kind == p.analysis.lane_for_kind


def test_cue_snapped_to_frame():
    p = Project()
    p.frame_rate_key = "25"
    c = editing.add_cue(p, p.lanes[0].id, 1.013)
    assert c.time == pytest.approx(1.0)


def test_undo_redo():
    p = Project()
    u = UndoStack(p)
    u.push("add")
    editing.add_cue(p, p.lanes[0].id, 1.0)
    u.push("add")
    editing.add_cue(p, p.lanes[0].id, 2.0)
    assert len(p.cues) == 2
    u.undo()
    assert len(p.cues) == 1
    u.undo()
    assert len(p.cues) == 0
    u.redo()
    assert len(p.cues) == 1
    assert u.is_dirty()


def test_accept_reject_and_merge():
    p = Project()
    s1 = Suggestion("hit", 1.0, 0.9, "kick")
    s2 = Suggestion("hit", 2.0, 0.9, "kick")
    s3 = Suggestion("section", 4.0, 0.9, "change", label="Section B")
    editing.merge_suggestions(p, [s1, s2, s3], {"hit", "section"})
    assert all(s.lane_id for s in p.suggestions)
    cue = editing.accept_suggestion(p, s1)
    assert cue.source == "ai-accepted" and s1.status == "accepted"
    assert cue.lane_id == p.lane_for_kind("hit")
    editing.reject_suggestion(p, s2)
    # accepting twice does nothing
    assert editing.accept_suggestion(p, s1) is None
    # Re-analysis: decided ones are not resurrected; pending ones replaced
    new = [Suggestion("hit", 1.01, 0.95, "kick"), Suggestion("hit", 2.0, 0.95, "kick"),
           Suggestion("hit", 3.0, 0.95, "snare")]
    added = editing.merge_suggestions(p, new, {"hit"})
    assert added == 1
    pending = [s for s in p.suggestions if s.status == "pending"]
    assert sorted(s.time for s in pending) == [3.0, 4.0]
    # confirmed cue untouched
    assert len(p.cues) == 1


def test_accept_does_not_duplicate_existing_cue():
    p = Project()
    lane = p.lane_for_kind("hit")
    c = editing.add_cue(p, lane, 1.0)
    s = Suggestion("hit", 1.01, 0.9, "kick", lane_id=lane)
    p.suggestions.append(s)
    got = editing.accept_suggestion(p, s)
    assert got is c and len(p.cues) == 1


def test_delete_accepted_cue_marks_rejected():
    p = Project()
    s = Suggestion("hit", 1.0, 0.9, "kick")
    editing.merge_suggestions(p, [s], {"hit"})
    cue = editing.accept_suggestion(p, s)
    editing.delete_cues(p, {cue.id})
    assert s.status == "rejected"


def test_threshold_filter():
    p = Project()
    p.analysis.thresholds["hit"] = 0.7
    editing.merge_suggestions(p, [Suggestion("hit", 1.0, 0.6, "a"), Suggestion("hit", 2.0, 0.8, "b")], {"hit"})
    assert [s.time for s in p.visible_suggestions()] == [2.0]
    p.analysis.visible["hit"] = False
    assert p.visible_suggestions() == []


def test_nudge_and_snap():
    p = Project()
    p.frame_rate_key = "30"
    p.beat_grid = BeatGrid.from_tempo(120, 0.0, 10)
    c = editing.add_cue(p, p.lanes[0].id, 1.1)
    editing.snap_cues_to_grid(p, {c.id})
    assert c.time == pytest.approx(1.0)
    editing.nudge_frames(p, {c.id}, 3)
    assert c.time == pytest.approx(1.1)
    editing.nudge_beats(p, {c.id}, 1)
    assert c.time == pytest.approx(1.5)


def test_effective_cue_numbers():
    p = Project()
    lane = p.lanes[0].id
    a = editing.add_cue(p, lane, 1)
    b = editing.add_cue(p, lane, 2)
    b.number = 5
    c = editing.add_cue(p, lane, 3)
    nums = editing.effective_cue_numbers(p, lane)
    assert nums[a.id] == 1 and nums[b.id] == 5 and nums[c.id] == 6
    editing.renumber_lane(p, lane, 10, 0.5)
    assert [x.number for x in p.cues_in_lane(lane)] == [10, 10.5, 11]


def test_grid_tools():
    from cueforge.core.grid_tools import double_tempo, grid_from_taps, halve_tempo, merge_grid
    g = BeatGrid.from_tempo(120, 0.0, 8.0, 4)
    h = halve_tempo(g)
    assert h.bpm() == pytest.approx(60) and h.beats_per_bar == 4
    assert h.downbeats[:2] == pytest.approx([0.0, 4.0])
    d = double_tempo(h)
    assert d.bpm() == pytest.approx(120) and d.downbeats[:2] == pytest.approx([0.0, 2.0])
    # taps: slightly off, one missed beat, one double tap
    true = np.arange(0, 6, 0.5)
    onsets = true + 0.002
    taps = list(true + np.random.default_rng(0).normal(0, 0.03, len(true)))
    del taps[5]
    taps.insert(3, taps[3] + 0.05)
    t = grid_from_taps(taps, onsets, 4)
    assert len(t.beats) == len(true)
    assert np.max(np.abs(np.asarray(t.beats) - onsets)) < 0.01
    assert t.downbeats[0] == pytest.approx(onsets[0]) and t.confirmed
    m = merge_grid(BeatGrid.from_tempo(100, 0.0, 20.0), BeatGrid.from_tempo(120, 4.0, 6.0))
    assert all(np.diff(m.beats) > 0)
