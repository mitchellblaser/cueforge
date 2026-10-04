"""Beat-grid editing helpers for live material (pure functions)."""
from __future__ import annotations

import numpy as np

from .model import BeatGrid


def _rebar(beats: np.ndarray, first_downbeat: float, bpb: int) -> list[float]:
    k0 = int(np.argmin(np.abs(beats - first_downbeat)))
    return [float(x) for x in beats[k0 % bpb::bpb]]


def halve_tempo(g: BeatGrid) -> BeatGrid:
    """Keep every other beat (the tracker locked onto 8th notes). The meter is kept,
    so bars become twice as long; bar 1 stays where it was."""
    if len(g.beats) < 4:
        return g
    b = np.asarray(g.beats)
    first = g.downbeats[g.bar_one_index()] if g.downbeats else b[0]     # bar 1 stays a beat
    k = int(np.argmin(np.abs(b - first)))
    keep = b[k % 2::2]
    return BeatGrid([float(x) for x in keep], _rebar(keep, first, g.beats_per_bar), g.beats_per_bar,
                    g.source + " (halved)", g.confirmed, g.confidence,
                    float(b[k]) if g.bar_one is not None else None)


def double_tempo(g: BeatGrid) -> BeatGrid:
    """Insert a beat half-way between beats (the tracker locked onto half notes)."""
    if len(g.beats) < 2:
        return g
    b = np.asarray(g.beats)
    beats = np.sort(np.r_[b, (b[:-1] + b[1:]) / 2])
    first = g.downbeats[g.bar_one_index()] if g.downbeats else b[0]
    return BeatGrid([float(x) for x in beats], _rebar(beats, first, g.beats_per_bar), g.beats_per_bar,
                    g.source + " (doubled)", g.confirmed, g.confidence, g.bar_one)


def grid_from_taps(taps: list[float], onsets: np.ndarray | None = None, beats_per_bar: int = 4,
                   snap_window: float = 0.07) -> BeatGrid:
    """Turn live taps into a beat grid.

    * Each tap snaps to the nearest detected onset within `snap_window` (people tap a
      little early or late; the drums are the reference).
    * Missed taps (gaps of ~2x, 3x the local beat length) are filled in.
    * The first tap is bar 1.
    """
    t = np.sort(np.asarray(taps, float))
    if len(t) < 2:
        return BeatGrid()
    if onsets is not None and len(onsets):
        on = np.sort(np.asarray(onsets, float))
        for i, x in enumerate(t):
            j = int(np.searchsorted(on, x))
            cands = [on[k] for k in (j - 1, j) if 0 <= k < len(on)]
            if cands:
                best = min(cands, key=lambda c: abs(c - x))
                if abs(best - x) <= snap_window:
                    t[i] = best
    t = t[np.r_[True, np.diff(t) > 0.15]]  # drop accidental double taps
    beats = [t[0]]
    for k in range(1, len(t)):
        recent = np.diff(beats[-9:]) if len(beats) > 2 else np.diff(t[:3])
        ibi = float(np.median(recent)) if len(recent) else t[k] - t[k - 1]
        gap = t[k] - beats[-1]
        n = int(round(gap / ibi)) if ibi > 0 else 1
        if n > 1 and abs(gap / n - ibi) < 0.15 * ibi:
            beats.extend(beats[-1] + gap * np.arange(1, n) / n)
        beats.append(t[k])
    beats = [float(x) for x in beats]
    downs = beats[::beats_per_bar]
    return BeatGrid(beats, downs, beats_per_bar, "tapped", confirmed=True, confidence=1.0, bar_one=beats[0])


def merge_grid(base: BeatGrid, patch: BeatGrid) -> BeatGrid:
    """Replace the part of `base` covered by `patch` (e.g. a tapped section)."""
    if not patch.beats:
        return base
    if not base.beats:
        return patch
    p = np.asarray(patch.beats)
    ibi = float(np.median(np.diff(p))) if len(p) > 1 else 0.5
    lo, hi = p[0] - 0.5 * ibi, p[-1] + 0.5 * ibi
    beats = [b for b in base.beats if b < lo or b > hi] + list(patch.beats)
    downs = [d for d in base.downbeats if d < lo or d > hi] + list(patch.downbeats)
    return BeatGrid(sorted(beats), sorted(downs), patch.beats_per_bar, f"{base.source} + tapped",
                    True, min(base.confidence or 1.0, 1.0), base.bar_one)


# ------------------------------------------------------------------ bar 1
def set_bar_one(g: BeatGrid, t: float, drop_before: bool = False) -> BeatGrid:
    """Make the beat nearest `t` the downbeat of bar 1: bars are re-phased from it in both
    directions and numbered from it. With `drop_before`, beats before it are removed (a
    silent or noisy intro the tracker filled with beats). The result is confirmed: the
    programmer decided."""
    if not g.beats:
        return g
    b = np.asarray(g.beats, float)
    j = int(np.argmin(np.abs(b - t)))
    if drop_before:
        b = b[j:]
        j = 0
    bpb = g.beats_per_bar
    downs = [float(x) for k, x in enumerate(b) if (k - j) % bpb == 0]
    return BeatGrid([float(x) for x in b], downs, bpb, g.source, True, g.confidence, float(b[j]))


def move_bar_one(g: BeatGrid, beats: int) -> BeatGrid:
    """Move bar 1 (and every bar line) by whole beats: -1 = one beat earlier."""
    if not g.beats:
        return g
    b = np.asarray(g.beats, float)
    first = g.downbeats[g.bar_one_index()] if g.downbeats else b[0]
    j = int(np.argmin(np.abs(b - first))) + beats
    j = max(0, min(len(b) - 1, j))
    return set_bar_one(g, float(b[j]))


def drop_beats_before_bar_one(g: BeatGrid) -> BeatGrid:
    if not g.downbeats:
        return g
    return set_bar_one(g, g.downbeats[g.bar_one_index()], drop_before=True)

