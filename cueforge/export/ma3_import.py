"""Read a grandMA3 timecode XML back into a song (round trip from the console).

Tolerant of layout differences: every element with a `Time` attribute inside a timecode
track counts as an event; its command comes from a child `RealtimeCmd` (Token/Status/Cue).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from ..core.model import LANE_COLORS, Cue, Lane, Project
from .ma3 import MA3_TICKS_PER_SECOND


@dataclass
class TcEvent:
    time: float
    token: str = "Goto"
    status: str = "On"
    cue: float | None = None
    name: str = ""


@dataclass
class TcTrack:
    name: str
    seq: int | None
    events: list[TcEvent] = field(default_factory=list)


@dataclass
class TcShow:
    name: str
    offset: float | None
    tracks: list[TcTrack] = field(default_factory=list)


def _seconds(v: str | None, ticks: bool = True) -> float | None:
    if v is None or v == "":
        return None
    v = v.strip().rstrip("s")
    try:
        if "." in v or "e" in v.lower():
            return float(v)
        n = int(v)
    except ValueError:
        return None
    # MA3 stores times in 1/2^24 s ticks; files written in seconds use small integers
    return n / MA3_TICKS_PER_SECOND if ticks else float(n)


def _uses_ticks(tc) -> bool:
    """Decide the time unit once per file: any integer time of a second's worth of ticks
    or more means the whole file is in ticks (a cue at 0.04 s is 671089 ticks)."""
    ints = []
    for el in tc.iter():
        for key in ("Time", "Offset", "Duration"):
            v = (el.get(key) or "").strip().rstrip("s")
            if v.lstrip("-").isdigit():
                ints.append(abs(int(v)))
    return bool(ints) and max(ints) >= 1_000_000


def _seq_from(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"Sequences?\.(?:[^.]*?)(\d+)(?:\.|$)", text)
    return int(m.group(1)) if m else None


def _cue_from(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"Cues?\.(?:Cue\s*)?([\d.]+)$", text)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _cue_from_handle(text: str | None) -> float | None:
    """ValCueDestination "12.12.0.5.0.2500" -> cue 2.5 (MA keeps cue numbers × 1000)."""
    if not text:
        return None
    last = text.strip().rsplit(".", 1)[-1]
    return int(last) / 1000 if last.isdigit() else None


def parse_timecode_xml(text: str) -> TcShow:
    root = ET.fromstring(text)
    tc = root if root.tag == "Timecode" else root.find(".//Timecode")
    if tc is None:
        raise ValueError("No <Timecode> in this file")
    ticks = _uses_ticks(tc)
    show = TcShow(tc.get("Name", "Timecode"), _seconds(tc.get("Offset"), ticks))
    for tr in tc.iter("Track"):
        track = TcTrack(tr.get("Name", ""), _seq_from(tr.get("Target")))
        for el in tr.iter():
            if el is tr or el.get("Time") is None or el.tag in ("TimeRange",):
                continue
            t = _seconds(el.get("Time"), ticks)
            if t is None:
                continue
            cmd = el.find("RealtimeCmd")
            attrs = cmd.attrib if cmd is not None else el.attrib
            token = attrs.get("ExecToken") or attrs.get("Token") or "Goto"
            cue = _cue_from(attrs.get("Cue")) if attrs.get("Cue") else _cue_from_handle(attrs.get("ValCueDestination"))
            name = el.get("CueDestination") or el.get("Name", "")
            if name == token:                         # MA names events after their token
                name = ""
            ev = TcEvent(t, token, attrs.get("Status", "On") or "On", cue, name)
            if track.seq is None:
                track.seq = _seq_from(attrs.get("Cue"))
            track.events.append(ev)
        track.events.sort(key=lambda e: e.time)
        show.tracks.append(track)
    if show.offset is None:                   # no Offset: times are show timecode (07:00:01)
        first = min((e.time for tr in show.tracks for e in tr.events), default=0.0)
        if first >= 3600:
            show.offset = float(int(first // 3600) * 3600)
            for tr in show.tracks:
                for e in tr.events:
                    e.time = round(e.time - show.offset, 6)
    return show


def show_to_cues(show: TcShow) -> list[tuple[int | None, str, Cue]]:
    """(sequence, track name, cue) for every event; Temp On/Off pairs become Temps."""
    out = []
    for tr in show.tracks:
        evs = tr.events
        used = set()
        last_cue: Cue | None = None
        for i, ev in enumerate(evs):
            if i in used:
                continue
            tok = ev.token.lower()
            off = ev.status.lower() == "off" or tok == "off"
            if off:
                # a release without its Temp On: old-style hold end for the previous cue
                if last_cue is not None and last_cue.duration is None and ev.time > last_cue.time:
                    last_cue.duration = round(ev.time - last_cue.time, 3)
                continue
            name = re.sub(r"\s*\(release\)$", "", ev.name)
            cue = Cue(lane_id="", time=round(ev.time, 6), label="" if re.fullmatch(r"Cue [\d.]+", name) else name,
                      number=ev.cue, source="manual", notes="imported from grandMA3")
            if tok == "temp":
                for j in range(i + 1, len(evs)):
                    e2 = evs[j]
                    if e2.token.lower() == "temp" and e2.status.lower() == "off" and \
                            (e2.cue is None or ev.cue is None or e2.cue == ev.cue):
                        cue.duration = round(e2.time - ev.time, 3)
                        used.add(j)
                        break
            out.append((tr.seq, tr.name, cue))
            last_cue = cue
    return out


def import_into_song(project: Project, show: TcShow, replace: bool = True, set_offset: bool = True) -> dict:
    """Put an imported show into the current song. Lanes are matched by MA3 sequence
    (minus the song's sequence offset); missing lanes are created."""
    song = project.song
    items = show_to_cues(show)
    lanes_used = []
    created = []
    for seq, tname, _ in items:
        if (seq, tname) in [(a, b) for a, b, _ in lanes_used]:
            continue
        from ..core.editing import sequence_number
        lane = next((l for l in project.lanes if seq is not None and sequence_number(project, l) == seq), None) \
            or next((l for l in project.lanes if l.name.lower() == (tname or "").lower()), None)
        base_seq = None if seq is None else seq - song.seq_offset
        if lane is None:
            n = len(project.lanes)
            lane = Lane(tname or f"Seq {seq}", LANE_COLORS[n % len(LANE_COLORS)], "", base_seq or n + 1)
            project.lanes.append(lane)
            created.append(lane.name)
        lanes_used.append((seq, tname, lane.id))
    lane_of = {(a, b): c for a, b, c in lanes_used}
    if replace:
        ids = set(lane_of.values())
        project.cues = [c for c in project.cues if c.lane_id not in ids]
    added = 0
    for seq, tname, cue in items:
        cue.lane_id = lane_of[(seq, tname)]
        if any(c.lane_id == cue.lane_id and abs(c.time - cue.time) < 0.02 for c in project.cues):
            continue
        project.cues.append(cue)
        added += 1
    project.sort_cues()
    if set_offset and show.offset is not None:
        song.tc_offset = show.offset
    return {"cues": added, "lanes_created": created, "tracks": len(show.tracks)}
