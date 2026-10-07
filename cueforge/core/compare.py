"""How well would the analysis have suggested the cues you actually programmed?

Compares an analysis result (before it was merged into suggestions, which drops anything
already on one of your cues) with the confirmed cues of finished songs. Gives, per
suggestion type, how many shown suggestions land on one of your cues and how many of your
cues they find, per lane how much of it the analysis covers, and a threshold per type learnt
from your cues instead of from accept/reject clicks.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .model import SUGGESTION_KINDS, Suggestion


@dataclass
class KindRow:
    kind: str
    shown: int = 0               # at the current threshold
    on_cues: int = 0             # of those, on one of your cues
    cues_found: int = 0          # your cues with a shown suggestion of this kind on them
    cues_reachable: int = 0      # … at any confidence
    threshold: float = 0.0
    suggested: float | None = None

    @property
    def precision(self) -> float:
        return self.on_cues / self.shown if self.shown else 0.0


@dataclass
class LaneRow:
    lane: str
    cues: int = 0
    found: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


@dataclass
class Report:
    songs: int = 0
    cues: int = 0
    found: int = 0               # your cues with any shown suggestion on them
    shown: int = 0
    minutes: float = 0.0
    kinds: list[KindRow] = field(default_factory=list)
    lanes: list[LaneRow] = field(default_factory=list)

    @property
    def per_minute(self) -> float:
        return self.shown / self.minutes if self.minutes else 0.0


def _tolerance(grid_beats: list[float], t: float) -> float:
    """A quarter of a beat at t (at least 80 ms): close enough to be the same cue."""
    if len(grid_beats) > 1:
        b = np.asarray(grid_beats)
        i = int(np.clip(np.searchsorted(b, t), 1, len(b) - 1))
        return max(0.08, 0.25 * float(b[i] - b[i - 1]))
    return 0.12


def compare(songs: list[tuple[object, list[Suggestion]]], lanes: list, thresholds: dict[str, float]) -> Report:
    """`songs`: (song, raw analysis suggestions) for every song to include."""
    from ..analysis.tuning import learn_thresholds
    lane_name = {l.id: l.name for l in lanes}
    rep = Report()
    rows = {k: KindRow(k, threshold=thresholds.get(k, 0.5)) for k in SUGGESTION_KINDS if k != "console"}
    lane_rows: dict[str, LaneRow] = {}
    history: list[dict] = []
    for song, raw in songs:
        cues = [c for c in song.cues]
        if not cues:
            continue
        rep.songs += 1
        rep.cues += len(cues)
        beats = song.beat_grid.beats
        end = max([c.time for c in cues] + [s.time for s in raw] + [0.0])
        rep.minutes += max(end, 1.0) / 60
        ct = np.asarray(sorted(c.time for c in cues))

        def near_cue(t: float) -> bool:
            j = int(np.searchsorted(ct, t))
            tol = _tolerance(beats, t)
            return any(0 <= k < len(ct) and abs(ct[k] - t) <= tol for k in (j - 1, j))
        for s in raw:
            if s.kind not in rows:
                continue
            hit = near_cue(s.time)
            history.append({"kind": s.kind, "confidence": s.confidence, "accepted": hit})
            r = rows[s.kind]
            if s.confidence >= r.threshold:
                r.shown += 1
                rep.shown += 1
                r.on_cues += hit
        for c in cues:
            tol = _tolerance(beats, c.time)
            lr = lane_rows.setdefault(c.lane_id, LaneRow(lane_name.get(c.lane_id, "?")))
            lr.cues += 1
            got = False
            for k, r in rows.items():
                near = [s for s in raw if s.kind == k and abs(s.time - c.time) <= tol]
                if near:
                    r.cues_reachable += 1
                if any(s.confidence >= r.threshold for s in near):
                    r.cues_found += 1
                    lr.by_kind[k] = lr.by_kind.get(k, 0) + 1
                    got = True
            if got:
                rep.found += 1
                lr.found += 1
    learnt = learn_thresholds(history, beta=1.0)
    for k, r in rows.items():
        if k in learnt:
            r.suggested = learnt[k]["threshold"]
    rep.kinds = [r for r in rows.values() if r.shown or r.cues_reachable or r.suggested is not None]
    rep.lanes = sorted(lane_rows.values(), key=lambda l: -l.cues)
    return rep
