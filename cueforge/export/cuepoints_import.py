"""Import cue lists from CuePoints (or any spreadsheet) CSV / TAB files.

CuePoints exports one row per cue, tab or comma separated, with a header row, e.g.

    Track       Type      Position     Cue No  Label  Fade
    RunBoyRun   Lighting  07:00:00:00  36      Bong   0

CuePoints "Tracks" become CueForge songs (matched by name, or created), "Types" become
lanes. Positions are absolute timecode: the song's start timecode is subtracted (and set
from the first cue's hour for a new song). Columns are recognised by name and can be
re-assigned in the import dialog, so other spreadsheets work too.
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field

from ..core.model import LANE_COLORS, Cue, Lane, Project

FIELDS = ("song", "lane", "time", "number", "label", "fade", "duration", "notes")
FIELD_LABELS = {"song": "Track / song", "lane": "Type / lane", "time": "Position (time)", "number": "Cue number",
                "label": "Label", "fade": "Fade", "duration": "Hold / duration", "notes": "Notes"}
ALIASES = {
    "song": ("track", "song", "track name", "song name", "title"),
    "lane": ("type", "cuepoint type", "cue type", "lane", "department", "category", "layer", "sequence"),
    "time": ("position", "time", "timecode", "tc", "start", "in", "cue time", "time code"),
    "number": ("cue no", "cue no.", "cue", "cue number", "cue #", "number", "no", "no.", "#", "q"),
    "label": ("label", "name", "cue name", "text", "description", "title"),
    "fade": ("fade", "fade time", "time in", "up"),
    "duration": ("duration", "length", "hold", "temp", "hold time"),
    "notes": ("notes", "note", "comment", "comments", "info"),
}


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]
    delimiter: str = "\t"


@dataclass
class ImportOptions:
    mapping: dict[str, int | None] = field(default_factory=dict)   # field -> column index
    by_song: bool = True            # rows go to the song named in the Track column (created if missing)
    absolute: bool = True           # positions are absolute timecode (subtract the song start)
    replace: bool = False           # replace existing cues in the lanes that receive cues
    default_lane: str = "Main Cues"


def read_table(text: str) -> Table:
    text = text.lstrip("﻿")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return Table([], [])
    sample = "\n".join(lines[:20])
    counts = {d: sample.count(d) for d in ("\t", ",", ";")}
    delim = max(counts, key=counts.get) if max(counts.values()) else ","
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    if not rows:
        return Table([], [], delim)
    first = rows[0]
    # a header row has only words: any number / timecode cell makes it data
    is_header = not any(re.fullmatch(r"[\d:;.,\-+ ]+", c) for c in first if c)
    if is_header:
        headers, body = first, rows[1:]
    else:
        headers, body = [f"Column {i + 1}" for i in range(len(first))], rows
    width = max(len(headers), max((len(r) for r in body), default=0))
    headers = headers + [f"Column {i + 1}" for i in range(len(headers), width)]
    body = [r + [""] * (width - len(r)) for r in body]
    return Table(headers, body, delim)


def guess_mapping(t: Table) -> dict[str, int | None]:
    norm = [re.sub(r"[\s_]+", " ", h.lower()).strip(" :") for h in t.headers]
    out: dict[str, int | None] = {}
    used: set[int] = set()
    for f in FIELDS:                         # exact names first, then partial matches
        idx = next((i for i, h in enumerate(norm) if i not in used and h in ALIASES[f]), None)
        if idx is None:
            idx = next((i for i, h in enumerate(norm) if i not in used and h and
                        any(h.startswith(a) for a in ALIASES[f] if len(a) > 2)), None)
        out[f] = idx
        if idx is not None:
            used.add(idx)
    if out["time"] is None:                  # headerless: the first column that looks like a time
        for i in range(len(t.headers)):
            vals = [r[i] for r in t.rows[:20] if r[i]]
            if vals and all(":" in v and parse_time(v, None) is not None for v in vals):
                out["time"] = i
                break
    return out


def parse_time(v: str, rate) -> float | None:
    """Timecode HH:MM:SS:FF (or ;FF), H:MM:SS.mmm, MM:SS(.mmm) or seconds."""
    v = v.strip()
    if not v:
        return None
    v = v.replace(",", ".") if v.count(",") == 1 and ":" not in v else v
    m = re.fullmatch(r"(-?)(\d{1,2})[:.](\d{1,2})[:.](\d{1,2})[:;](\d{1,3})", v)
    if m:                                    # HH:MM:SS:FF (badly padded fields are fine)
        sign = -1 if m.group(1) else 1
        h, mi, s, fr = (int(m.group(k)) for k in range(2, 6))
        if rate is not None:
            from ..core.timecode import parse_tc
            try:
                return sign * parse_tc(f"{h:02d}:{mi:02d}:{s:02d}:{fr:02d}", rate, 0.0)
            except ValueError:
                pass
        return sign * (h * 3600 + mi * 60 + s + fr / float(getattr(rate, "nominal", 30) or 30))
    m = re.fullmatch(r"(-?)(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", v)
    if m:
        sign = -1 if m.group(1) else 1
        h = int(m.group(2) or 0)
        return sign * (h * 3600 + int(m.group(3)) * 60 + float(m.group(4)))
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def _num(v: str) -> float | None:
    try:
        x = float(v.replace(",", ".")) if v else None
        return x if x is None or math.isfinite(x) else None
    except ValueError:
        return None


@dataclass
class Row:
    song: str
    lane: str
    time: float
    number: float | None
    label: str
    fade: float | None
    duration: float | None
    notes: str


def rows(t: Table, opts: ImportOptions, rate) -> tuple[list[Row], list[str]]:
    m = opts.mapping
    out, problems = [], []

    def get(r, f):
        i = m.get(f)
        return r[i] if i is not None and i < len(r) else ""
    for k, r in enumerate(t.rows, 2 if t.headers and not t.headers[0].startswith("Column ") else 1):
        tm = parse_time(get(r, "time"), rate)
        if tm is None:
            problems.append(f"Row {k}: no usable time ({get(r, 'time') or 'empty'!r}) — skipped")
            continue
        dur = _num(get(r, "duration"))
        out.append(Row(get(r, "song"), get(r, "lane") or opts.default_lane, tm, _num(get(r, "number")),
                       get(r, "label"), _num(get(r, "fade")), dur if dur and dur > 0 else None, get(r, "notes")))
    return out, problems


def group_by_song(items: list[Row], opts: ImportOptions) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = {}
    for r in items:
        groups.setdefault(r.song.strip() if opts.by_song else "", []).append(r)
    return groups


def target_song(project: Project, name: str, group: list[Row], opts: ImportOptions):
    """The song a group of rows goes to: matched by name, created if missing (a fresh empty
    project's "Song 1" is reused). Returns (song, created)."""
    if not (opts.by_song and name):
        return project.song, False
    song = next((s for s in project.songs if s.name.strip().lower() == name.lower()), None)
    if song is not None:
        return song, False
    fresh = len(project.songs) == 1 and not project.songs[0].cues and not project.songs[0].tracks
    song = project.songs[0] if fresh else project.add_song(name)
    song.name = name
    if opts.absolute:                       # CuePoints tracks usually start on an hour (07:00:00:00)
        first = min(r.time for r in group)
        song.tc_offset = float(int(first // 3600) * 3600) if first >= 3600 else 0.0
    return song, True


def add_rows(project: Project, group: list[Row], opts: ImportOptions) -> dict:
    """Add rows to the current song. Returns {"cues", "lanes_created", "skipped"}."""
    song = project.song
    offset = song.tc_offset if opts.absolute else 0.0
    lanes_created, touched, new_cues = [], set(), []
    skipped = 0
    for r in group:
        lane = next((l for l in project.lanes if l.name.strip().lower() == r.lane.strip().lower()), None)
        if lane is None:
            n = len(project.lanes)
            seq = max((l.ma3_sequence for l in project.lanes), default=0) + 1
            lane = Lane(r.lane, LANE_COLORS[n % len(LANE_COLORS)], "", seq)
            project.lanes.append(lane)
            lanes_created.append(lane.name)
        t = r.time - offset
        if t < 0:
            skipped += 1
            continue
        touched.add(lane.id)
        new_cues.append(Cue(lane_id=lane.id, time=round(t, 6), label=r.label, number=r.number, fade=r.fade,
                            duration=r.duration, notes=r.notes or "imported from CuePoints", source="manual"))
    if opts.replace:
        project.cues = [c for c in project.cues if c.lane_id not in touched]
    added = 0
    for c in new_cues:
        if any(x.lane_id == c.lane_id and abs(x.time - c.time) < 0.02 for x in project.cues):
            skipped += 1
            continue
        project.cues.append(c)
        added += 1
    project.sort_cues()
    return {"cues": added, "lanes_created": lanes_created, "skipped": skipped}


def import_rows(project: Project, items: list[Row], opts: ImportOptions) -> dict:
    """Add all rows (songs by Track name). Returns a summary."""
    total = {"cues": 0, "songs_created": [], "lanes_created": [], "skipped": 0}
    current = project.song.id
    for name, group in group_by_song(items, opts).items():
        project.select_song(current)        # rows without a Track go to the song that was open
        song, created = target_song(project, name, group, opts)
        if created:
            total["songs_created"].append(song.name)
        project.select_song(song.id)
        r = add_rows(project, group, opts)
        total["cues"] += r["cues"]
        total["skipped"] += r["skipped"]
        total["lanes_created"] += r["lanes_created"]
    project.select_song(current)
    return total
