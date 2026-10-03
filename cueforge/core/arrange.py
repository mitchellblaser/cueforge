"""Arranging cues: copy/paste, section repeats, pattern fill (pure functions).

Times are mapped through the beat grid when there is one, so a chorus copied onto
the next chorus lands on the same beats even if a live band has drifted in tempo.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np

from .model import BeatGrid, Cue, Project, SECTION_COLORS, SectionMarker
from .timecode import snap_to_frame

SAME_CUE = 0.02   # cues closer than this in one lane are the same cue


def _tol(project: Project) -> float:
    """Range edges allow half a frame: cues are frame-snapped and may sit just before."""
    return 0.5 / project.frame_rate.fps + 1e-3


# ------------------------------------------------------------------ beat mapping
def beat_at(grid: BeatGrid, t: float) -> float | None:
    """Fractional beat index at time t (extrapolated past the grid ends)."""
    b = grid.beats
    if len(b) < 2:
        return None
    i = bisect.bisect_right(b, t) - 1
    i = max(0, min(len(b) - 2, i))
    return i + (t - b[i]) / (b[i + 1] - b[i])


def time_at(grid: BeatGrid, beat: float) -> float | None:
    b = grid.beats
    if len(b) < 2:
        return None
    i = int(np.floor(beat))
    i = max(0, min(len(b) - 2, i))
    return b[i] + (beat - i) * (b[i + 1] - b[i])


# ------------------------------------------------------------------ sections
def section_bounds(project: Project, marker: SectionMarker, song_end: float) -> tuple[float, float]:
    ms = sorted(project.sections, key=lambda m: m.time)
    i = next(k for k, m in enumerate(ms) if m.id == marker.id)
    end = ms[i + 1].time if i + 1 < len(ms) else song_end
    return marker.time, end


def section_at(project: Project, t: float) -> SectionMarker | None:
    ms = [m for m in sorted(project.sections, key=lambda m: m.time) if m.time <= t + 1e-6]
    return ms[-1] if ms else None


def repeats_of(project: Project, marker: SectionMarker) -> list[SectionMarker]:
    return [m for m in sorted(project.sections, key=lambda m: m.time) if m.kind == marker.kind and m.id != marker.id]


def add_section(project: Project, t: float, name: str = "") -> SectionMarker:
    t = snap_to_frame(max(0.0, t), project.frame_rate)
    existing = next((m for m in project.sections if abs(m.time - t) < 1e-3), None)
    if existing:
        return existing
    n = len(project.sections)
    m = SectionMarker(name or f"Section {n + 1}", t, SECTION_COLORS[n % len(SECTION_COLORS)])
    project.sections.append(m)
    project.sections.sort(key=lambda m: m.time)
    return m


def recolor_by_kind(project: Project) -> None:
    """Repeats of a section share a colour."""
    kinds: dict[str, str] = {}
    for m in sorted(project.sections, key=lambda m: m.time):
        if m.kind not in kinds:
            kinds[m.kind] = SECTION_COLORS[len(kinds) % len(SECTION_COLORS)]
        m.color = kinds[m.kind]


def sections_from_suggestions(project: Project, min_confidence: float = 0.0) -> list[SectionMarker]:
    """Turn the AI's section-change suggestions into editable markers, named by the
    repeat letter it found ("Part A 1", "Part B 1", "Part A 2" …)."""
    import re
    sugs = sorted((s for s in project.suggestions if s.kind == "section" and s.confidence >= min_confidence
                   and s.status != "rejected"), key=lambda s: s.time)
    counts: dict[str, int] = {}
    out = []
    first = SectionMarker("Intro", 0.0)
    project.sections = [first]
    for s in sugs:
        m = re.search(r"Section ([A-Z])", s.label or "")
        letter = m.group(1) if m else "?"
        counts[letter] = counts.get(letter, 0) + 1
        out.append(add_section(project, s.time, f"Part {letter} {counts[letter]}"))
    recolor_by_kind(project)
    return out


# ------------------------------------------------------------------ clipboard
@dataclass
class ClipCue:
    lane_id: str
    rel: float                 # seconds after the anchor
    rel_beats: float | None    # beats after the anchor (when a grid exists)
    label: str = ""
    duration: float | None = None
    dur_beats: float | None = None
    fade: float | None = None
    notes: str = ""


@dataclass
class Clipboard:
    cues: list[ClipCue] = field(default_factory=list)
    # offset of the anchor from the start of its section (for "paste into section")
    section_rel: float = 0.0
    section_rel_beats: float | None = None

    @property
    def empty(self) -> bool:
        return not self.cues


def copy_cues(project: Project, cue_ids: set[str], anchor: float | None = None) -> Clipboard:
    cues = sorted((c for c in project.cues if c.id in cue_ids), key=lambda c: c.time)
    if not cues:
        return Clipboard()
    g = project.beat_grid
    a = cues[0].time if anchor is None else anchor
    ab = beat_at(g, a)
    clip = Clipboard()
    for c in cues:
        cb = beat_at(g, c.time)
        db = None
        if c.duration and ab is not None:
            db = beat_at(g, c.time + c.duration) - cb
        clip.cues.append(ClipCue(c.lane_id, c.time - a, None if ab is None else cb - ab, c.label, c.duration, db,
                                 c.fade, c.notes))
    sec = section_at(project, a)
    if sec is not None:
        clip.section_rel = a - sec.time
        sb = beat_at(g, sec.time)
        clip.section_rel_beats = None if (ab is None or sb is None) else ab - sb
    return clip


def paste(project: Project, clip: Clipboard, anchor: float, end: float | None = None,
          use_beats: bool = True, lane_override: str | None = None) -> list[Cue]:
    """Paste so the clipboard anchor lands at `anchor`. Cues past `end` are dropped.
    Cues that would duplicate an existing cue in the same lane are skipped."""
    g = project.beat_grid
    ab = beat_at(g, anchor) if use_beats else None
    made = []
    for cc in clip.cues:
        if ab is not None and cc.rel_beats is not None:
            t = time_at(g, ab + cc.rel_beats)
            dur = cc.duration
            if cc.dur_beats is not None:
                dur = time_at(g, ab + cc.rel_beats + cc.dur_beats) - t
        else:
            t, dur = anchor + cc.rel, cc.duration
        if t is None or t < 0 or (end is not None and t >= end - 1e-6):
            continue
        t = snap_to_frame(t, project.frame_rate)
        lane = lane_override or cc.lane_id
        if not project.lane(lane):
            continue
        if any(c.lane_id == lane and abs(c.time - t) < SAME_CUE for c in project.cues):
            continue
        cue = Cue(lane_id=lane, time=t, label=cc.label, fade=cc.fade, notes=cc.notes,
                  duration=round(dur, 3) if dur else None)
        project.cues.append(cue)
        made.append(cue)
    project.sort_cues()
    return made


def section_anchor(project: Project, marker: SectionMarker, clip: Clipboard) -> float:
    """Where the clipboard anchor goes when pasting into `marker`'s section, keeping the
    copied cues' position relative to their own section start."""
    g = project.beat_grid
    if clip.section_rel_beats is not None:
        sb = beat_at(g, marker.time)
        if sb is not None:
            return time_at(g, sb + clip.section_rel_beats)
    return marker.time + clip.section_rel


def copy_section_to(project: Project, src: SectionMarker, targets: list[SectionMarker], song_end: float,
                    lane_ids: set[str] | None = None, replace: bool = False) -> int:
    """Copy the cues inside `src` onto each target section (aligned by beats from the
    section start, cut at the target's end). Returns the number of cues created."""
    s0, s1 = section_bounds(project, src, song_end)
    tol = _tol(project)
    ids = {c.id for c in project.cues if s0 - tol <= c.time < s1 - tol
           and (lane_ids is None or c.lane_id in lane_ids)}
    if not ids:
        return 0
    clip = copy_cues(project, ids, anchor=s0)
    n = 0
    for tgt in targets:
        t0, t1 = section_bounds(project, tgt, song_end)
        if replace:
            lanes = {cc.lane_id for cc in clip.cues}
            project.cues = [c for c in project.cues if not (c.lane_id in lanes and t0 - tol <= c.time < t1 - tol)]
        n += len(paste(project, clip, t0, end=t1))
    return n


# ------------------------------------------------------------------ pattern fill
PATTERN_STEPS = {"bar": None, "2 beats": 2.0, "beat": 1.0, "1/2 beat (8ths)": 0.5, "1/3 beat (triplets)": 1 / 3,
                 "1/4 beat (16ths)": 0.25}


def pattern_fill(project: Project, lane_id: str, t0: float, t1: float, step: str | float = "beat",
                 offset_beats: float = 0.0, temp_hold: float | None = None, replace: bool = False,
                 label: str = "", seconds_step: float | None = None) -> list[Cue]:
    """Drop a cue (or Temp, if temp_hold) every `step` between t0 and t1 in a lane.

    With a beat grid the steps follow the grid (bars use the downbeats); without one,
    `seconds_step` (or 0.5 s) is used."""
    g = project.beat_grid
    times: list[float] = []
    if step == "bar" and g.downbeats:
        times = [d for d in g.downbeats if t0 - 1e-6 <= d < t1 - 1e-6]
        if offset_beats:
            times = [time_at(g, beat_at(g, d) + offset_beats) for d in times]
    elif g.beats and len(g.beats) > 1:
        stepb = PATTERN_STEPS.get(step, step) if isinstance(step, str) else float(step)
        stepb = float(stepb or g.beats_per_bar)
        b0 = beat_at(g, t0)
        # start on the first grid step at/after t0
        k = np.ceil((b0 - offset_beats) / stepb - 1e-6)
        b = k * stepb + offset_beats
        while True:
            t = time_at(g, b)
            if t is None or t >= t1 - 1e-6:
                break
            if t >= t0 - 1e-6:
                times.append(t)
            b += stepb
    else:
        dt = seconds_step or 0.5
        t = t0
        while t < t1 - 1e-6:
            times.append(t)
            t += dt
    if replace:
        tol = _tol(project)
        project.cues = [c for c in project.cues if not (c.lane_id == lane_id and t0 - tol <= c.time < t1 - tol)]
    made = []
    for k, t in enumerate(times):
        t = snap_to_frame(t, project.frame_rate)
        if any(c.lane_id == lane_id and abs(c.time - t) < SAME_CUE for c in project.cues):
            continue
        cue = Cue(lane_id=lane_id, time=t, label=label.replace("{n}", str(k + 1)) if label else "",
                  duration=temp_hold if temp_hold else None)
        project.cues.append(cue)
        made.append(cue)
    project.sort_cues()
    return made
