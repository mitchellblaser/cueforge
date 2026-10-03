"""Project data model (pure Python, no Qt)."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .timecode import DEFAULT_RATE, FrameRate, get_rate

TRACK_ROLES = ["Track", "Stem", "Click", "Cue/Guide", "Other"]
ANALYSED_ROLES = {"Track", "Stem"}

SUGGESTION_KINDS = ["hit", "section", "energy"]
KIND_LABELS = {"hit": "Hits", "section": "Sections", "energy": "Energy"}

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


@dataclass
class Cue:
    lane_id: str
    time: float
    label: str = ""
    number: float | None = None  # MA3 cue number; None = auto-number on export
    fade: float | None = None
    notes: str = ""
    source: str = "manual"       # "manual" | "ai-accepted"
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
    id: str = field(default_factory=new_id)


@dataclass
class BeatGrid:
    beats: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    beats_per_bar: int = 4
    source: str = ""             # "manual", "click track", "librosa", "beat_this" ...
    confirmed: bool = False      # detected grids are suggestions until confirmed
    confidence: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.beats

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
        return cls(beats, downbeats, beats_per_bar, source, confirmed=True, confidence=1.0)

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
                        self.beats_per_bar, self.source, self.confirmed, self.confidence)


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
    thresholds: dict[str, float] = field(default_factory=lambda: {"hit": 0.75, "section": 0.5, "energy": 0.6})
    visible: dict[str, bool] = field(default_factory=lambda: {"hit": True, "section": True, "energy": True})
    lane_for_kind: dict[str, str] = field(default_factory=dict)  # kind -> lane id
    snap_to_grid: bool = True
    snap_window: float = 0.07    # seconds; suggestions this close to a beat snap to it


@dataclass
class ExportSettings:
    ma3_name: str = ""
    ma3_time_unit: str = "ticks"     # "ticks" (1/16777216 s) or "seconds"
    ma3_data_version: str = "2.1.1.5"
    ltc_sample_rate: int = 48000
    ltc_level_db: float = -12.0
    ltc_preroll: float = 2.0


class Project:
    """Everything that is saved to a .cueproj file."""

    def __init__(self) -> None:
        self.name = "Untitled"
        self.path: str = ""
        self.frame_rate_key = DEFAULT_RATE
        self.tc_offset = 0.0        # timecode at song time 0, in seconds (e.g. 3600 = 01:00:00:00)
        self.tracks: list[Track] = []
        self.lanes: list[Lane] = []
        self.cues: list[Cue] = []
        self.suggestions: list[Suggestion] = []
        self.beat_grid = BeatGrid()
        self.mixer = MixerState()
        self.analysis = AnalysisSettings()
        self.export = ExportSettings()
        self.loop: tuple[float, float] | None = None
        self.view: dict[str, Any] = {}
        self.add_default_lanes()

    # -- helpers --------------------------------------------------------
    @property
    def frame_rate(self) -> FrameRate:
        return get_rate(self.frame_rate_key)

    def add_default_lanes(self) -> None:
        self.lanes = [
            Lane("Main Cues", LANE_COLORS[0], "1", 1),
            Lane("Hits", LANE_COLORS[1], "2", 2),
            Lane("Strobe", LANE_COLORS[2], "3", 3),
        ]
        self.analysis.lane_for_kind = {
            "section": self.lanes[0].id, "energy": self.lanes[0].id, "hit": self.lanes[1].id}

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
        return {
            "lanes": [asdict(l) for l in self.lanes],
            "cues": [asdict(c) for c in self.cues],
            "suggestions": [asdict(s) for s in self.suggestions],
            "beat_grid": asdict(self.beat_grid),
            "lane_for_kind": dict(self.analysis.lane_for_kind),
        }

    def restore_edit_state(self, st: dict[str, Any]) -> None:
        self.lanes = [_from_dict(Lane, d) for d in st["lanes"]]
        self.cues = [_from_dict(Cue, d) for d in st["cues"]]
        self.suggestions = [_from_dict(Suggestion, d) for d in st["suggestions"]]
        self.beat_grid = _from_dict(BeatGrid, st["beat_grid"])
        self.analysis.lane_for_kind = dict(st["lane_for_kind"])

    # -- serialisation --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = self.edit_state()
        d.update({
            "format": "cueforge-project",
            "version": 1,
            "name": self.name,
            "frame_rate": self.frame_rate_key,
            "tc_offset": self.tc_offset,
            "tracks": [asdict(t) for t in self.tracks],
            "mixer": asdict(self.mixer),
            "analysis": asdict(self.analysis),
            "export": asdict(self.export),
            "loop": list(self.loop) if self.loop else None,
            "view": self.view,
        })
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        p = cls()
        p.name = d.get("name", "Untitled")
        p.frame_rate_key = d.get("frame_rate", DEFAULT_RATE)
        p.tc_offset = float(d.get("tc_offset", 0.0))
        p.tracks = [_from_dict(Track, t) for t in d.get("tracks", [])]
        p.mixer = _from_dict(MixerState, d.get("mixer", {}))
        p.analysis = _from_dict(AnalysisSettings, d.get("analysis", {}))
        p.export = _from_dict(ExportSettings, d.get("export", {}))
        loop = d.get("loop")
        p.loop = (float(loop[0]), float(loop[1])) if loop else None
        p.view = d.get("view", {}) or {}
        p.restore_edit_state({
            "lanes": d.get("lanes", []),
            "cues": d.get("cues", []),
            "suggestions": d.get("suggestions", []),
            "beat_grid": d.get("beat_grid", {}),
            "lane_for_kind": d.get("analysis", {}).get("lane_for_kind", {}),
        })
        if not p.lanes:
            p.add_default_lanes()
        return p
