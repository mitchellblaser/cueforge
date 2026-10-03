import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge.core import arrange, editing
from cueforge.core.model import BeatGrid, Project, SectionMarker, Suggestion


def drifting_grid():
    """Live-ish grid: 120 BPM speeding up to ~126 BPM."""
    ibis = np.linspace(0.5, 0.476, 200)
    beats = np.r_[0.0, np.cumsum(ibis)]
    return BeatGrid([float(b) for b in beats], [float(b) for b in beats[::4]], 4, "test", True)


def song_with_sections():
    p = Project()
    p.beat_grid = drifting_grid()
    b = p.beat_grid.beats
    for name, bar in (("Verse 1", 0), ("Chorus 1", 8), ("Verse 2", 16), ("Chorus 2", 24), ("Outro", 32)):
        arrange.add_section(p, b[bar * 4], name)
    return p


def test_beat_mapping_roundtrip():
    g = drifting_grid()
    for t in (0.0, 3.3, 50.0):
        assert arrange.time_at(g, arrange.beat_at(g, t)) == pytest.approx(t, abs=1e-6)


def test_section_kinds_and_repeats():
    p = song_with_sections()
    ch1 = next(m for m in p.sections if m.name == "Chorus 1")
    assert ch1.kind == "chorus"
    assert [m.name for m in arrange.repeats_of(p, ch1)] == ["Chorus 2"]
    assert arrange.section_at(p, p.beat_grid.beats[40]).name == "Chorus 1"


def test_copy_section_to_repeats_follows_beats():
    p = song_with_sections()
    g = p.beat_grid
    lane = p.lanes[0].id
    ch1 = next(m for m in p.sections if m.name == "Chorus 1")
    ch2 = next(m for m in p.sections if m.name == "Chorus 2")
    # cues on beat 0, beat 6 and a Temp on beat 13.5 (1 beat hold) of chorus 1
    for rel in (0, 6):
        editing.add_cue(p, lane, g.beats[32 + rel])
    t = arrange.time_at(g, 32 + 13.5)
    c = editing.add_cue(p, lane, t)
    c.duration = arrange.time_at(g, 32 + 14.5) - c.time
    n = arrange.copy_section_to(p, ch1, [ch2], song_end=g.beats[-1])
    assert n == 3
    new = [x for x in p.cues if x.time >= ch2.time - 0.02]
    beats = sorted(arrange.beat_at(g, x.time) for x in new)
    assert beats == pytest.approx([96.0, 102.0, 109.5], abs=0.08)   # same beats (within a frame)
    temp = max(new, key=lambda x: x.time)
    assert arrange.beat_at(g, temp.time + temp.duration) - arrange.beat_at(g, temp.time) == pytest.approx(1, abs=0.05)
    # running again doesn't duplicate
    assert arrange.copy_section_to(p, ch1, [ch2], song_end=g.beats[-1]) == 0


def test_paste_at_playhead_and_into_section():
    p = song_with_sections()
    g = p.beat_grid
    lane = p.lanes[1].id
    a = editing.add_cue(p, lane, g.beats[34], label="hit")      # chorus 1, beat 2
    b = editing.add_cue(p, lane, g.beats[36], label="hit2")
    clip = arrange.copy_cues(p, {a.id, b.id})
    assert clip.section_rel_beats == pytest.approx(2, abs=0.08)
    made = arrange.paste(p, clip, g.beats[150])
    assert [round(arrange.beat_at(g, x.time)) for x in made] == [150, 152]
    ch2 = next(m for m in p.sections if m.name == "Chorus 2")
    anchor = arrange.section_anchor(p, ch2, clip)
    made = arrange.paste(p, clip, anchor)
    assert [round(arrange.beat_at(g, x.time)) for x in made] == [98, 100]
    assert [x.label for x in made] == ["hit", "hit2"]


def test_pattern_fill():
    p = song_with_sections()
    g = p.beat_grid
    lane = p.lanes[2].id
    t0, t1 = g.beats[32], g.beats[64]                     # chorus 1 (8 bars)
    made = arrange.pattern_fill(p, lane, t0, t1, "bar")
    assert len(made) == 8
    made = arrange.pattern_fill(p, lane, t0, t1, "1/2 beat (8ths)", temp_hold=0.1, replace=True, label="Chase {n}")
    assert len(made) == 64 and made[1].label == "Chase 2" and all(c.duration == 0.1 for c in made)
    assert len(p.cues_in_lane(lane)) == 64
    off = arrange.pattern_fill(p, p.lanes[3].id, t0, g.beats[36], "beat", offset_beats=0.5)
    assert [arrange.beat_at(g, c.time) for c in off] == pytest.approx([32.5, 33.5, 34.5, 35.5], abs=0.08)
    # no grid: seconds
    q = Project()
    assert len(arrange.pattern_fill(q, q.lanes[0].id, 0, 2, "beat", seconds_step=0.25)) == 8


def test_sections_from_ai_suggestions():
    p = Project()
    for t, lab in ((10, "Section B"), (20, "Section C (high energy)"), (30, "Section B"), (40, "Section C")):
        p.suggestions.append(Suggestion("section", t, 0.8, "x", label=lab))
    arrange.sections_from_suggestions(p)
    assert [m.name for m in p.sections] == ["Intro", "Part B 1", "Part C 1", "Part B 2", "Part C 2"]
    b1 = p.sections[1]
    assert [m.name for m in arrange.repeats_of(p, b1)] == ["Part B 2"]
    assert p.sections[1].color == p.sections[3].color != p.sections[2].color
