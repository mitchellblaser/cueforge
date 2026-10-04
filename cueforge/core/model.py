"""Project data model (pure Python, no Qt)."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .timecode import DEFAULT_RATE, FrameRate, get_rate

TRACK_ROLES = ["Track", "Stem", "Click", "Cue/Guide", "Other"]
ANALYSED_ROLES = {"Track", "Stem"}

SUGGESTION_KINDS = ["hit", "fill", "section", "energy", "harmony", "melody"]
KIND_LABELS = {"hit": "Hits", "fill": "Drum fills", "section": "Sections", "energy": "Energy",
               "harmony": "Chord changes", "melody": "Lead lines"}
# lane each suggestion kind goes to by default: (lane name, tap key)
KIND_DEFAULT_LANE = {"hit": ("Hits", "2"), "fill": ("Strobe", "3"), "section": ("Main Cues", "1"),
                     "energy": ("Main Cues", "1"), "harmony": ("Colour", "4"), "melody": ("FX / Chase", "5")}

LANE_COLORS = ["#4FC3F7", "#FFB74D", "#E57373", "#81C784", "#BA68C8", "#FFD54F", "#4DB6AC", "#F06292"]
TRACK_COLORS = ["#90A4AE", "#7986CB", "#4DD0E1", "#AED581", "#FF8A65", "#A1887F", "#9575CD", "#DCE775"]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _from_dict(cls, data: dict[str, Any]):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Track:
    name: str
    path: str
    role: str = "Track"
    offset: float = 0.0          # seconds; positive = track starts later on the timeline
    gain_db: float = 0.0
    mute: bool = False
    solo: bool = False
    analyse: bool = True
    color: str = TRACK_COLORS[0]
    id: str = field(default_factory=new_id)


@dataclass
class Lane:
    name: str
    color: str = LANE_COLORS[0]
    tap_key: str = ""            # key that taps a cue into this lane
    ma3_sequence: int = 1        # target sequence on export
    export: bool = True
    id: str = field(default_factory=new_id)
    # own sequence per song (song's sequence offset added, cues from 1) or one sequence shared by
    # every song (hits, strobes). None = decide by name (projects from before the option)
    per_song: bool | None = None


GLOBAL_LANE_WORDS = ("hit", "strobe")


def lane_per_song(lane: Lane) -> bool:
    """Does this lane get its own MA3 sequence in every song? Hits / strobe lanes default to
    one shared sequence; everything else is per song."""
    if lane.per_song is not None:
        return bool(lane.per_song)
    return not any(w in lane.name.lower() for w in GLOBAL_LANE_WORDS)


@dataclass
class Cue:
    lane_id: str
    time: float
    label: str = ""
    number: float | None = None  # MA3 cue number; None = auto-number on export
    fade: float | None = None
    notes: str = ""
    source: str = "manual"       # "manual" | "ai-accepted"
    duration: float | None = None  # seconds the look is held (e.g. strobe through a fill); off afterwards
    id: str = field(default_factory=new_id)


@dataclass
class Suggestion:
    kind: str                    # "hit" | "section" | "energy"
    time: float
    confidence: float
    reason: str
    lane_id: str = ""            # lane the cue goes into when accepted
    label: str = ""
    status: str = "pending"      # "pending" | "accepted" | "rejected"
    cue_id: str = ""
    source_track: str = ""
    duration: float = 0.0        # span (fills, phrases)
    idea: str = ""               # what the programmer might do here
    steps: list[float] = field(default_factory=list)  # note times (lead lines) for chase steps
    id: str = field(default_factory=new_id)


@dataclass
class BeatGrid:
    beats: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    beats_per_bar: int = 4
    source: str = ""             # "manual", "click track", "librosa", "beat_this" ...
    confirmed: bool = False      # detected grids are suggestions until confirmed
    confidence: float = 0.0
    bar_one: float | None = None  # downbeat numbered bar 1 (earlier bars are 0, -1 … count-in)

    @property
    def empty(self) -> bool:
        return not self.beats

    def bar_one_index(self) -> int:
        """Index into `downbeats` of bar 1."""
        if self.bar_one is None or not self.downbeats:
            return 0
        import bisect
        i = bisect.bisect_left(self.downbeats, self.bar_one - 1e-3)
        cands = [j for j in (i - 1, i) if 0 <= j < len(self.downbeats)]
        return min(cands, key=lambda j: abs(self.downbeats[j] - self.bar_one))

    def bar_number(self, downbeat_index: int) -> int:
        """Musical bar number of downbeats[downbeat_index] (bar 1 = `bar_one`)."""
        return downbeat_index - self.bar_one_index() + 1

    def bpm(self) -> float:
        if len(self.beats) < 2:
            return 0.0
        import numpy as np
        return float(60.0 / np.median(np.diff(self.beats)))

    @classmethod
    def from_tempo(cls, bpm: float, first_downbeat: float, duration: float,
                   beats_per_bar: int = 4, source: str = "manual") -> "BeatGrid":
        period = 60.0 / bpm
        t = first_downbeat
        while t - period >= 0:
            t -= period
        beats, downbeats = [], []
        # index relative to first downbeat so downbeats stay aligned
        idx0 = -round((first_downbeat - t) / period)
        i = 0
        while t <= duration + 1e-9:
            beats.append(round(t, 6))
            if (idx0 + i) % beats_per_bar == 0:
                downbeats.append(round(t, 6))
            t += period
            i += 1
        return cls(beats, downbeats, beats_per_bar, source, confirmed=True, confidence=1.0,
                   bar_one=round(first_downbeat, 6))

    def nearest_beat(self, t: float) -> float | None:
        if not self.beats:
            return None
        import bisect
        i = bisect.bisect_left(self.beats, t)
        cands = [self.beats[j] for j in (i - 1, i) if 0 <= j < len(self.beats)]
        return min(cands, key=lambda b: abs(b - t))

    def step(self, t: float, direction: int) -> float | None:
        """Next/previous beat strictly after/before t."""
        if not self.beats:
            return None
        import bisect
        if direction > 0:
            i = bisect.bisect_right(self.beats, t + 1e-6)
            return self.beats[i] if i < len(self.beats) else None
        i = bisect.bisect_left(self.beats, t - 1e-6) - 1
        return self.beats[i] if i >= 0 else None

    def shifted(self, delta: float) -> "BeatGrid":
        return BeatGrid([b + delta for b in self.beats], [d + delta for d in self.downbeats],
                        self.beats_per_bar, self.source, self.confirmed, self.confidence,
                        None if self.bar_one is None else self.bar_one + delta)


@dataclass
class MixerState:
    master_db: float = 0.0
    click_enabled: bool = False
    click_db: float = -6.0
    blips_enabled: bool = False
    blips_db: float = -10.0


@dataclass
class AnalysisSettings:
    # Minimum confidence shown per suggestion kind (filter, no re-analysis needed)
    thresholds: dict[str, float] = field(default_factory=lambda: {
        "hit": 0.75, "fill": 0.6, "section": 0.5, "energy": 0.6, "harmony": 0.6, "melody": 0.5})
    visible: dict[str, bool] = field(default_factory=lambda: {k: True for k in SUGGESTION_KINDS})
    lane_for_kind: dict[str, str] = field(default_factory=dict)  # kind -> lane id
    snap_to_grid: bool = True
    snap_window: float = 0.07    # seconds; suggestions this close to a beat snap to it


@dataclass
class ExportSettings:
    ma3_name: str = ""
    ma3_time_unit: str = "seconds"   # unused: grandMA3 timecode XML is always in seconds
    ma3_data_version: str = "2.1.1.5"
    ma3_cue_token: str = "Go+"        # command for normal cues: "Go+" or "Goto"
    ma3_first_goto: bool = True       # first cue of each lane is a Goto (resyncs the sequence)
    ltc_sample_rate: int = 48000
    ltc_level_db: float = -12.0
    ltc_preroll: float = 2.0


SECTION_COLORS = ["#5C6BC0", "#26A69A", "#EF5350", "#AB47BC", "#FFA726", "#66BB6A", "#29B6F6", "#EC407A"]


@dataclass
class SectionMarker:
    """A song section (verse, chorus…). It runs to the next marker, or to `end` when that
    comes first (the last section of a song, or a gap before the next one)."""
    name: str
    time: float
    color: str = ""
    id: str = field(default_factory=new_id)
    end: float | None = None

    @property
    def kind(self) -> str:
        """Sections with the same kind are repeats: "Chorus 2" -> "chorus"."""
        import re
        return re.sub(r"[\s#]*\d+$", "", self.name).strip().lower() or self.name.lower()


class Song:
    """One song in the setlist: its own audio, cues, suggestions, grid and timecode."""

    def __init__(self, name: str = "Song 1") -> None:
        self.id = new_id()
        self.name = name
        self.tracks: list[Track] = []
        self.cues: list[Cue] = []
        self.suggestions: list[Suggestion] = []
        self.beat_grid = BeatGrid()
        self.mixer = MixerState()
        self.loop: tuple[float, float] | None = None
        self.view: dict[str, Any] = {}
        self.tc_offset = 0.0         # timecode at song time 0, in seconds (e.g. 3600 = 01:00:00:00)
        self.ma3_timecode = 1        # grandMA3 Timecode pool slot for this song
        self.seq_offset = 0          # added to per-song lanes' MA3 sequence numbers for this song
        self.cue_start = 1.0         # first auto cue number for this song in shared (global) lanes
        self.notes = ""
        self.sections: list[SectionMarker] = []
        self.analysed = ""           # when the AI analysis last ran (ISO time), "" = never

    def duration_hint(self) -> float:
        end = max((c.time + (c.duration or 0) for c in self.cues), default=0.0)
        return end

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "notes": self.notes, "analysed": self.analysed,
            "tracks": [asdict(t) for t in self.tracks],
            "cues": [asdict(c) for c in self.cues],
            "suggestions": [asdict(s) for s in self.suggestions],
            "beat_grid": asdict(self.beat_grid),
            "mixer": asdict(self.mixer),
            "loop": list(self.loop) if self.loop else None,
            "view": self.view,
            "tc_offset": self.tc_offset, "ma3_timecode": self.ma3_timecode,
            "seq_offset": self.seq_offset, "cue_start": self.cue_start,
            "sections": [asdict(m) for m in self.sections],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Song":
        s = cls(d.get("name", "Song"))
        s.id = d.get("id") or new_id()
        s.notes = d.get("notes", "")
        s.analysed = d.get("analysed", "")
        s.tracks = [_from_dict(Track, t) for t in d.get("tracks", [])]
        s.restore_content(d)
        s.mixer = _from_dict(MixerState, d.get("mixer", {}))
        loop = d.get("loop")
        s.loop = (float(loop[0]), float(loop[1])) if loop else None
        s.view = d.get("view", {}) or {}
        s.tc_offset = float(d.get("tc_offset", 0.0))
        s.ma3_timecode = int(d.get("ma3_timecode", 1))
        s.seq_offset = int(d.get("seq_offset", 0))
        s.cue_start = float(d.get("cue_start", 1.0))
        return s

    def content(self) -> dict[str, Any]:
        # fast field copies (lists copied one level); dataclasses.asdict's deep recursion
        # made every edit slow on big songs
        return {"cues": [_snap(c) for c in self.cues],
                "suggestions": [_snap(x) for x in self.suggestions],
                "beat_grid": _snap(self.beat_grid),
                "sections": [_snap(m) for m in self.sections]}

    def restore_content(self, d: dict[str, Any]) -> None:
        self.cues = [_from_dict(Cue, c) for c in d.get("cues", [])]
        self.suggestions = [_from_dict(Suggestion, x) for x in d.get("suggestions", [])]
        self.beat_grid = _from_dict(BeatGrid, d.get("beat_grid", {}))
        self.sections = sorted((_from_dict(SectionMarker, m) for m in d.get("sections", [])),
                               key=lambda m: m.time)


def _snap(obj) -> dict[str, Any]:
    d = dict(obj.__dict__)
    for k, v in d.items():
        if isinstance(v, list):
            d[k] = [list(x) if isinstance(x, list) else dict(x) if isinstance(x, dict) else x for x in v]
        elif isinstance(v, dict):
            d[k] = dict(v)
    return d


def _song_attr(name: str):
    """Project attribute that lives on the current song."""
    def get(self):
        return getattr(self.song, name)

    def set_(self, value):
        setattr(self.song, name, value)
    return property(get, set_)


class Project:
    """Everything that is saved to a .cueproj file: a setlist of songs sharing lanes
    (MA3 sequences), frame rate and analysis settings."""

    tracks = _song_attr("tracks")
    cues = _song_attr("cues")
    suggestions = _song_attr("suggestions")
    beat_grid = _song_attr("beat_grid")
    mixer = _song_attr("mixer")
    loop = _song_attr("loop")
    view = _song_attr("view")
    tc_offset = _song_attr("tc_offset")
    sections = _song_attr("sections")

    def __init__(self) -> None:
        self.name = "Untitled"
        self.path: str = ""
        self.frame_rate_key = DEFAULT_RATE
        self.songs: list[Song] = [Song("Song 1")]
        self.current = 0
        self.lanes: list[Lane] = []
        self.analysis = AnalysisSettings()
        self.export = ExportSettings()
        # what the grandMA3 live link has already created on the console (not undoable:
        # it records the console's state): {"cues": {cue id: [seq, number, label]}, "seqs": {seq: name}}
        self.console: dict[str, Any] = {}
        self.snap_div = 1                # snap to beats (1), half beats (2) or quarter beats (4)
        self.add_default_lanes()

    # -- songs ------------------------------------------------------------
    @property
    def song(self) -> Song:
        if not self.songs:
            self.songs.append(Song("Song 1"))
        self.current = max(0, min(self.current, len(self.songs) - 1))
        return self.songs[self.current]

    def song_by_id(self, sid: str) -> Song | None:
        return next((s for s in self.songs if s.id == sid), None)

    def select_song(self, sid: str) -> bool:
        for i, s in enumerate(self.songs):
            if s.id == sid:
                self.current = i
                return True
        return False

    def add_song(self, name: str = "") -> Song:
        n = len(self.songs) + 1
        s = Song(name or f"Song {n}")
        prev = self.songs[-1] if self.songs else None
        # sensible show defaults: song N starts at N:00:00:00, own timecode slot, own cue range
        s.tc_offset = (prev.tc_offset + 3600.0) if prev else 0.0
        s.ma3_timecode = (max(x.ma3_timecode for x in self.songs) + 1) if self.songs else 1
        s.cue_start = float(100 * (n - 1) + 1)
        # per-song lanes: song 2's Main Cues goes to sequence 1 + 100 and so on (stored, so
        # reordering the setlist never moves a song to other sequences)
        s.seq_offset = (max(x.seq_offset for x in self.songs) + 100) if self.songs else 0
        self.songs.append(s)
        return s

    def remove_song(self, sid: str) -> None:
        self.songs = [s for s in self.songs if s.id != sid] or [Song("Song 1")]
        self.current = min(self.current, len(self.songs) - 1)

    def move_song(self, sid: str, direction: int) -> None:
        i = next((k for k, s in enumerate(self.songs) if s.id == sid), -1)
        j = i + direction
        if 0 <= i < len(self.songs) and 0 <= j < len(self.songs):
            cur = self.song.id
            self.songs[i], self.songs[j] = self.songs[j], self.songs[i]
            self.select_song(cur)

    def all_tracks(self) -> list[Track]:
        return [t for s in self.songs for t in s.tracks]

    # -- helpers --------------------------------------------------------
    @property
    def frame_rate(self) -> FrameRate:
        return get_rate(self.frame_rate_key)

    def add_default_lanes(self) -> None:
        self.lanes = []
        self.analysis.lane_for_kind = {}
        for kind in ("section", "hit", "fill", "harmony", "melody", "energy"):
            self.ensure_kind_lane(kind)

    def ensure_kind_lane(self, kind: str) -> str:
        """Lane for a suggestion kind; creates the default lane if it is missing."""
        lid = self.analysis.lane_for_kind.get(kind, "")
        if self.lane(lid):
            return lid
        name, key = KIND_DEFAULT_LANE.get(kind, ("Main Cues", "1"))
        lane = next((l for l in self.lanes if l.name == name), None)
        if lane is None:
            used = {l.tap_key for l in self.lanes}
            n = len(self.lanes)
            lane = Lane(name, LANE_COLORS[n % len(LANE_COLORS)], key if key not in used else "",
                        max((l.ma3_sequence for l in self.lanes), default=0) + 1,
                        per_song=kind not in ("hit", "fill"))
            self.lanes.append(lane)
        self.analysis.lane_for_kind[kind] = lane.id
        return lane.id

    def lane(self, lane_id: str) -> Lane | None:
        return next((l for l in self.lanes if l.id == lane_id), None)

    def track(self, track_id: str) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def cue(self, cue_id: str) -> Cue | None:
        return next((c for c in self.cues if c.id == cue_id), None)

    def suggestion(self, sid: str) -> Suggestion | None:
        return next((s for s in self.suggestions if s.id == sid), None)

    def cues_in_lane(self, lane_id: str) -> list[Cue]:
        return sorted((c for c in self.cues if c.lane_id == lane_id), key=lambda c: c.time)

    def sort_cues(self) -> None:
        self.cues.sort(key=lambda c: c.time)

    def lane_for_kind(self, kind: str) -> str:
        lid = self.analysis.lane_for_kind.get(kind, "")
        if not self.lane(lid):
            lid = self.lanes[0].id if self.lanes else ""
        return lid

    def visible_suggestions(self) -> list[Suggestion]:
        a = self.analysis
        return [s for s in self.suggestions
                if s.status == "pending" and a.visible.get(s.kind, True)
                and s.confidence >= a.thresholds.get(s.kind, 0.0)]

    # -- edit snapshots (for undo) -------------------------------------
    def edit_state(self) -> dict[str, Any]:
        st = self.song.content()
        st.update({"song_id": self.song.id,
                   "lanes": [asdict(l) for l in self.lanes],
                   "lane_for_kind": dict(self.analysis.lane_for_kind)})
        return st

    def restore_edit_state(self, st: dict[str, Any]) -> None:
        if st.get("song_id"):
            self.select_song(st["song_id"])  # undo jumps back to the song it happened in
        self.song.restore_content(st)
        self.lanes = [_from_dict(Lane, d) for d in st["lanes"]]
        self.analysis.lane_for_kind = dict(st["lane_for_kind"])

    # -- serialisation --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "cueforge-project",
            "version": 2,
            "name": self.name,
            "frame_rate": self.frame_rate_key,
            "lanes": [asdict(l) for l in self.lanes],
            "analysis": asdict(self.analysis),
            "export": asdict(self.export),
            "songs": [s.to_dict() for s in self.songs],
            "current": self.current,
            "console": self.console,
            "snap_div": self.snap_div,
            "per_song_seqs": True,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        p = cls()
        p.name = d.get("name", "Untitled")
        p.frame_rate_key = d.get("frame_rate", DEFAULT_RATE)
        p.analysis = _from_dict(AnalysisSettings, d.get("analysis", {}))
        defaults = AnalysisSettings()
        for k in SUGGESTION_KINDS:  # projects saved before a kind existed
            p.analysis.thresholds.setdefault(k, defaults.thresholds[k])
            p.analysis.visible.setdefault(k, True)
        p.export = _from_dict(ExportSettings, d.get("export", {}))
        p.console = d.get("console") or {}
        p.snap_div = int(d.get("snap_div", 1) or 1)
        if "songs" in d:
            p.songs = [Song.from_dict(x) for x in d["songs"]] or [Song("Song 1")]
            p.current = int(d.get("current", 0))
        else:  # version 1: a single song stored at the top level
            song = Song.from_dict({**d, "name": d.get("name", "Song 1")})
            p.songs = [song]
            p.current = 0
        p.lanes = [_from_dict(Lane, x) for x in d.get("lanes", [])]
        p.analysis.lane_for_kind = dict(d.get("analysis", {}).get("lane_for_kind", {}))
        if not p.lanes:
            p.add_default_lanes()
        if not d.get("per_song_seqs") and len(p.songs) > 1:
            # from before per-song sequences: give each song its own block of sequences, and let
            # per-song lanes number from 1 again — numbers the live link fixed (101, 102 … in
            # song 2) are cleared; numbers typed by hand are kept
            if not any(s.seq_offset for s in p.songs):
                for i, s in enumerate(p.songs):
                    s.seq_offset = 100 * i
            from .editing import _number_plain
            pinned = p.console.get("cues", {})
            for s in p.songs:
                if s.cue_start == 1:
                    continue
                for lane in (l for l in p.lanes if lane_per_song(l)):
                    cues = sorted((c for c in s.cues if c.lane_id == lane.id), key=lambda c: c.time)
                    # a link-fixed number is exactly what automatic numbering gave the cue
                    maybe = {c.id for c in cues if c.number is not None and c.id in pinned}
                    plain = [c for c in cues if not c.duration]
                    keep = {c.id: c.number for c in plain}
                    for c in plain:
                        if c.id in maybe:
                            c.number = None
                    auto = _number_plain(s.cue_start, plain)
                    for c in plain:
                        c.number = None if (c.id in maybe and auto[c.id] == keep[c.id]) else keep[c.id]
                    temps = [c for c in cues if c.duration and c.id in maybe]
                    top = max((n for n in auto.values()), default=None)
                    shared = s.cue_start if top is None else float(int(top) + 1)
                    for c in temps:
                        if c.number == shared:
                            c.number = None
        return p
