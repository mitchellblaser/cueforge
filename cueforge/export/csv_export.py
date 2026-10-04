"""CSV cue list (for paperwork / spreadsheets)."""
from __future__ import annotations

import csv

from ..core.editing import effective_cue_numbers, sequence_number
from ..core.model import Project
from ..core.timecode import seconds_to_tc


def export_csv(project: Project, path: str, lane_ids: list[str] | None = None, all_songs: bool = False) -> int:
    from .ma3 import in_song
    rate = project.frame_rate
    lanes = [l for l in project.lanes if lane_ids is None or l.id in lane_ids]
    rows = []
    for si, song in enumerate(project.songs if all_songs else [project.song]):
        with in_song(project, song):
            song_rows = []
            for lane in lanes:
                nums = effective_cue_numbers(project, lane.id)
                for c in project.cues_in_lane(lane.id):
                    song_rows.append((si, c.time, lane, c, nums[c.id], song, sequence_number(project, lane)))
            rows += sorted(song_rows, key=lambda r: r[1])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Song", "Lane", "MA3 Sequence", "Cue", "Label", "Timecode", "Seconds", "Fade", "Temp hold", "Source",
                    "Notes"])
        for _, t, lane, c, num, song, seq in rows:
            project_offset = song.tc_offset
            w.writerow([song.name, lane.name, seq, f"{num:g}", c.label,
                        seconds_to_tc(t, rate, project_offset), f"{t:.3f}",
                        "" if c.fade is None else f"{c.fade:g}", "" if not c.duration else f"{c.duration:.3f}",
                        c.source, c.notes])
    return len(rows)
