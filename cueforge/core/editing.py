"""Edit operations on a Project. Pure functions; callers push undo first."""
from __future__ import annotations

from .model import Cue, Project, Suggestion
from .timecode import snap_to_frame

DUPLICATE_WINDOW = 0.05  # seconds: cues/suggestions this close are considered the same


def add_cue(project: Project, lane_id: str, time: float, snap_grid: bool = False,
            label: str = "", source: str = "manual") -> Cue:
    t = snap_time(project, time, snap_grid)
    cue = Cue(lane_id=lane_id, time=t, label=label, source=source)
    project.cues.append(cue)
    project.sort_cues()
    return cue


def snap_time(project: Project, t: float, snap_grid: bool) -> float:
    if snap_grid and project.beat_grid.beats:
        b = project.beat_grid.nearest_beat(t)
        if b is not None:
            t = b
    return max(0.0, snap_to_frame(t, project.frame_rate))


def delete_cues(project: Project, cue_ids: set[str]) -> None:
    project.cues = [c for c in project.cues if c.id not in cue_ids]
    # If an accepted suggestion's cue is deleted, mark the suggestion rejected so it
    # does not come back as a pending ghost.
    for s in project.suggestions:
        if s.cue_id in cue_ids:
            s.status = "rejected"
            s.cue_id = ""


def move_cues(project: Project, cue_ids: set[str], delta: float) -> None:
    for c in project.cues:
        if c.id in cue_ids:
            c.time = max(0.0, snap_to_frame(c.time + delta, project.frame_rate))
    project.sort_cues()


def nudge_frames(project: Project, cue_ids: set[str], frames: int) -> None:
    move_cues(project, cue_ids, frames / project.frame_rate.fps)


def nudge_beats(project: Project, cue_ids: set[str], direction: int) -> None:
    grid = project.beat_grid
    if not grid.beats:
        nudge_frames(project, cue_ids, direction * round(project.frame_rate.fps / 2))
        return
    for c in project.cues:
        if c.id in cue_ids:
            nb = grid.step(c.time, direction)
            if nb is not None:
                c.time = snap_to_frame(nb, project.frame_rate)
    project.sort_cues()


def snap_cues_to_grid(project: Project, cue_ids: set[str]) -> None:
    for c in project.cues:
        if c.id in cue_ids:
            c.time = snap_time(project, c.time, True)
    project.sort_cues()


def move_cues_to_lane(project: Project, cue_ids: set[str], lane_id: str) -> None:
    for c in project.cues:
        if c.id in cue_ids:
            c.lane_id = lane_id


def renumber_lane(project: Project, lane_id: str, start: float = 1.0, step: float = 1.0) -> None:
    n = start
    for c in project.cues_in_lane(lane_id):
        c.number = round(n, 3)
        n += step


def effective_cue_numbers(project: Project, lane_id: str) -> dict[str, float]:
    """Cue numbers used on export: explicit numbers kept, others filled in ascending."""
    result: dict[str, float] = {}
    last = 0.0
    used = {c.number for c in project.cues_in_lane(lane_id) if c.number is not None}
    for c in project.cues_in_lane(lane_id):
        if c.number is not None:
            last = c.number
            result[c.id] = c.number
            continue
        n = float(int(last) + 1)
        while n in used:
            n += 1
        used.add(n)
        result[c.id] = n
        last = n
    return result


# -- suggestions -----------------------------------------------------------

def accept_suggestion(project: Project, s: Suggestion) -> Cue | None:
    if s.status != "pending":
        return None
    lane_id = s.lane_id if project.lane(s.lane_id) else project.ensure_kind_lane(s.kind)
    t = snap_to_frame(s.time, project.frame_rate)
    existing = next((c for c in project.cues
                     if c.lane_id == lane_id and abs(c.time - t) < DUPLICATE_WINDOW), None)
    if existing:
        cue = existing
    else:
        note = f"AI: {s.reason} ({s.confidence:.0%})"
        if s.idea:
            note += f" — idea: {s.idea}"
        cue = Cue(lane_id=lane_id, time=t, label=s.label, source="ai-accepted", notes=note,
                  duration=round(s.duration, 3) if s.duration > 0 else None)
        project.cues.append(cue)
        project.sort_cues()
    s.status = "accepted"
    s.cue_id = cue.id
    return cue


def accept_as_steps(project: Project, s: Suggestion, lane_id: str | None = None) -> list[Cue]:
    """Accept a lead-line suggestion as one cue per note (chase steps)."""
    if s.status != "pending" or not s.steps:
        return []
    lane_id = lane_id or (s.lane_id if project.lane(s.lane_id) else project.ensure_kind_lane(s.kind))
    cues = []
    for k, t in enumerate(s.steps):
        tt = snap_to_frame(t, project.frame_rate)
        if any(c.lane_id == lane_id and abs(c.time - tt) < 1e-6 for c in project.cues):
            continue
        cues.append(Cue(lane_id=lane_id, time=tt, label=str(k + 1),
                        source="ai-accepted", notes=f"AI chase step {k + 1}/{len(s.steps)}: {s.reason}"))
    project.cues.extend(cues)
    project.sort_cues()
    s.status = "accepted"
    s.cue_id = cues[0].id if cues else ""
    return cues


def reject_suggestion(project: Project, s: Suggestion) -> None:
    if s.status == "pending":
        s.status = "rejected"


def accept_many(project: Project, suggestions: list[Suggestion]) -> int:
    return sum(1 for s in suggestions if accept_suggestion(project, s))


def reject_many(project: Project, suggestions: list[Suggestion]) -> int:
    n = 0
    for s in suggestions:
        if s.status == "pending":
            s.status = "rejected"
            n += 1
    return n


def merge_suggestions(project: Project, new: list[Suggestion], kinds: set[str]) -> int:
    """Replace pending suggestions of the given kinds with fresh ones.

    Accepted and rejected decisions are kept, and new suggestions that match a
    previous decision (same kind, close in time) or an existing cue in the target
    lane are dropped, so re-analysing never resurrects something the programmer
    already dealt with.
    """
    keep = [s for s in project.suggestions if s.kind not in kinds or s.status != "pending"]
    decided = [s for s in keep if s.status in ("accepted", "rejected")]
    added = 0
    for s in new:
        if not s.lane_id:
            s.lane_id = project.ensure_kind_lane(s.kind)
        if any(d.kind == s.kind and abs(d.time - s.time) < DUPLICATE_WINDOW for d in decided):
            continue
        if any(c.lane_id == s.lane_id and abs(c.time - s.time) < DUPLICATE_WINDOW for c in project.cues):
            continue
        keep.append(s)
        added += 1
    keep.sort(key=lambda s: s.time)
    project.suggestions = keep
    return added


def next_suggestion(project: Project, t: float, direction: int = 1) -> Suggestion | None:
    vis = project.visible_suggestions()
    if direction > 0:
        return next((s for s in vis if s.time > t + 1e-3), None)
    prev = [s for s in vis if s.time < t - 1e-3]
    return prev[-1] if prev else None
