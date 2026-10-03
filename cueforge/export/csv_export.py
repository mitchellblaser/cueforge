"""CSV cue list (for paperwork / spreadsheets)."""
from __future__ import annotations

import csv

from ..core.editing import effective_cue_numbers
from ..core.model import Project
from ..core.timecode import seconds_to_tc


def export_csv(project: Project, path: str, lane_ids: list[str] | None = None) -> int:
    rate = project.frame_rate
    lanes = [l for l in project.lanes if lane_ids is None or l.id in lane_ids]
    rows = []
    for lane in lanes:
        nums = effective_cue_numbers(project, lane.id)
        for c in project.cues_in_lane(lane.id):
            rows.append((c.time, lane, c, nums[c.id]))
    rows.sort(key=lambda r: r[0])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Lane", "MA3 Sequence", "Cue", "Label", "Timecode", "Seconds", "Fade", "Hold", "Source", "Notes"])
        for t, lane, c, num in rows:
            w.writerow([lane.name, lane.ma3_sequence, f"{num:g}", c.label,
                        seconds_to_tc(t, rate, project.tc_offset), f"{t:.3f}",
                        "" if c.fade is None else f"{c.fade:g}", "" if not c.duration else f"{c.duration:.3f}",
                        c.source, c.notes])
    return len(rows)
