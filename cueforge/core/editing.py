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


def grid_point(project: Project, t: float, div: int | None = None) -> float | None:
    """Nearest grid point to t: beats, or 1/2, 1/4 beats (project.snap_div)."""
    g = project.beat_grid
    if not g.beats:
        return None
    div = int(div or getattr(project, "snap_div", 1) or 1)
    if div <= 1 or len(g.beats) < 2:
        return g.nearest_beat(t)
    from .arrange import beat_at, time_at
    b = beat_at(g, t)
    return None if b is None else time_at(g, round(b * div) / div)


def snap_time(project: Project, t: float, snap_grid: bool) -> float:
    if snap_grid and project.beat_grid.beats:
        b = grid_point(project, t)
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
    cues = project.cues_in_lane(lane_id)
    for c in cues:
        if not c.duration:
            c.number = round(n, 3)
            n += step
    temps = [c for c in cues if c.duration]       # all Temps fire one cue
    for c in temps:
        c.number = round(n, 3)


def effective_cue_numbers(project: Project, lane_id: str) -> dict[str, float]:
    """Cue numbers used on export: explicit numbers kept, others filled in ascending.

    Temps don't get a cue each: every Temp in a lane fires the same cue (the lane's Temp
    cue) with Temp On / Temp Off, so they share one number. In a per-song lane that is the
    earliest Temp's fixed number, else the next whole number after the lane's normal cues
    (1 in a Temp-only lane). In a lane shared by every song it is one cue for the whole
    setlist (shared_temp_number); that lane's normal cues skip it.

    Normal cues inserted before an explicitly numbered one share out the gap as point
    numbers (5.1, 5.2 …) so numbers stay in time order and numbers already on the console
    never shift. If a gap is too small even for 1/1000 steps, the leftovers continue past
    the fixed cue (cue_number_clashes reports it)."""
    from .model import lane_per_song
    cues = project.cues_in_lane(lane_id)
    plain = [c for c in cues if not c.duration]
    temps = [c for c in cues if c.duration]
    start = first_cue_number(project, lane_id)
    lane = project.lane(lane_id)
    if temps and lane is not None and not lane_per_song(lane):
        shared = shared_temp_number(project, lane_id)
        result = _number_plain(start, plain, taken={shared})
        for c in temps:
            result[c.id] = shared
        return result
    result = _number_plain(start, plain)
    if temps:
        shared = next((c.number for c in temps if c.number is not None), None)
        if shared is None:
            top = max(result.values(), default=None)
            shared = float(start) if top is None else float(int(top) + 1)
        for c in temps:
            result[c.id] = shared
    return result


def temp_cue_label(project: Project, lane_id: str) -> str:
    """The label of a lane's shared Temp cue: the first Temp label, else the lane name."""
    lane = project.lane(lane_id)
    return next((c.label for c in project.cues_in_lane(lane_id) if c.duration and c.label),
                lane.name if lane else "")


def shared_temp_number(project: Project, lane_id: str) -> float:
    """The one Temp cue a shared lane fires in every song: the number fixed on a Temp in that
    lane (earliest song in the setlist first), else cue 1."""
    for song in project.songs:
        temps = sorted((c for c in song.cues if c.lane_id == lane_id and c.duration), key=lambda c: c.time)
        fixed = next((c.number for c in temps if c.number is not None), None)
        if fixed is not None:
            return float(fixed)
    return 1.0


def first_cue_number(project: Project, lane_id: str) -> float:
    """Where automatic cue numbers start: 1 in a per-song lane (the song has the sequence to
    itself), the song's own range (101, 201 …) in a lane shared by every song."""
    from .model import lane_per_song
    lane = project.lane(lane_id)
    return 1.0 if lane is not None and lane_per_song(lane) else float(project.song.cue_start)


def sequence_name(project: Project, lane, song=None) -> str:
    """The lane's grandMA3 sequence in a song, identified by name: "<song> <lane>" for a
    per-song lane, the lane name for a lane shared by every song. Sequences are found on the
    console by this name, so they can sit at any number and be moved around there."""
    from .model import lane_per_song
    song = song or project.song
    name = f"{song.name} {lane.name}" if lane_per_song(lane) else lane.name
    return name.replace('"', "'").strip()


def sequence_plan(project: Project) -> dict[str, int]:
    """{sequence name: number} for the whole setlist, counting up from the project's
    Sequence start: shared lanes first (lane order), then each song's own lanes (setlist
    order). The file export uses these numbers; the live link and the plugin create a
    missing sequence in the first free number from its planned one, so CueForge's
    sequences sit together. Commands find sequences by name, not by number."""
    from .model import lane_per_song
    lanes = [l for l in project.lanes if l.export]
    used = {(s.id, c.lane_id) for s in project.songs for c in s.cues}   # only sequences that will exist
    names = [sequence_name(project, l) for l in lanes
             if not lane_per_song(l) and any((s.id, l.id) in used for s in project.songs)]
    for song in project.songs:
        names += [sequence_name(project, l, song) for l in lanes if lane_per_song(l) and (song.id, l.id) in used]
    start = max(1, int(getattr(project.export, "ma3_seq_start", 1) or 1))
    plan: dict[str, int] = {}
    for n in names:
        if n not in plan:
            plan[n] = start + len(plan)
    return plan


def sequence_number(project: Project, lane, song=None) -> int:
    """The planned number of the lane's sequence in a song (see sequence_plan)."""
    name = sequence_name(project, lane, song)
    plan = sequence_plan(project)
    return plan.get(name, max(1, int(getattr(project.export, "ma3_seq_start", 1) or 1)))


def _number_plain(start: float, cues: list, taken: set[float] | None = None) -> dict[str, float]:
    result: dict[str, float] = {}
    last = start - 1
    used = {c.number for c in cues if c.number is not None} | set(taken or ())
    i = 0
    while i < len(cues):
        c = cues[i]
        if c.number is not None:
            last = c.number
            result[c.id] = c.number
            i += 1
            continue
        j = i
        while j < len(cues) and cues[j].number is None:
            j += 1
        run = cues[i:j]                                    # unnumbered cues before the next fixed one
        nxt = cues[j].number if j < len(cues) else None
        whole = []
        n = float(int(last) + 1)
        for _ in run:
            while n in used:
                n += 1
            whole.append(n)
            n += 1
        if nxt is not None and whole and whole[-1] >= nxt:
            nums = None
            for step in (1.0, 0.1, 0.01, 0.001):
                first = (int(round(last / step)) + 1) * step
                cand = [round(first + k * step, 3) for k in range(len(run))]
                if cand[-1] < nxt - 1e-9 and not used.intersection(cand):
                    nums = cand
                    break
            if nums is None:                                # even 1/1000 steps don't fit
                gap = (nxt - last) / (len(run) + 1)
                cand = [round(last + gap * (k + 1), 3) for k in range(len(run))]
                ok = len(set(cand)) == len(cand) and cand[0] > last and cand[-1] < nxt and not used.intersection(cand)
                nums = cand if ok else whole
        else:
            nums = whole
        for cue, num in zip(run, nums):
            result[cue.id] = num
            used.add(num)
        last = nums[-1]
        i = j
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
