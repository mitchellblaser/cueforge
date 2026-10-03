"""Runs the analysis steps and turns results into Suggestions.

Nothing here touches confirmed cues: the result is applied by the caller via
`editing.merge_suggestions` (suggestions) and as an *unconfirmed* beat grid.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..audio.loader import ANALYSIS_SR, AudioData, resample
from ..core.model import ANALYSED_ROLES, BeatGrid, Project, Suggestion
from . import beats as beats_mod
from .energy import detect_energy
from .fills import detect_fills
from .harmony import detect_chord_changes
from .hits import detect_hits
from .melody import detect_phrases, is_lead_like
from .spectral import Spectra
from .stems import demucs_available, separate
from .structure import detect_sections, detect_sections_allin1

DRUM_WORDS = ("drum", "kick", "snare", "perc", "beat", "kit")


class Cancelled(Exception):
    pass


@dataclass
class AnalysisOptions:
    grid: bool = True
    hits: bool = True
    fills: bool = True
    sections: bool = True
    energy: bool = True
    harmony: bool = True
    melody: bool = True
    use_deep_models: bool = True     # Beat This! / All-In-One when installed
    use_demucs: bool = False         # separate the mix when no stems are imported
    melody_from_mix: bool = False    # rough lead-line estimate when there are no stems (slow)
    beats_per_bar: int = 0           # 0 = detect (3 or 4)


@dataclass
class AnalysisResult:
    grid: BeatGrid | None = None
    suggestions: list[Suggestion] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)
    log: list[str] = field(default_factory=list)


def _timeline_mix(items: list[tuple[AudioData, float]], sr: int, stereo: bool = False) -> np.ndarray:
    if not items:
        return np.zeros((0, 2) if stereo else 0, np.float32)
    end = max(off + a.duration for a, off in items)
    n = int(end * sr) + 1
    out = np.zeros((n, 2) if stereo else n, np.float32)
    for a, off in items:
        x = a.samples if stereo else a.mono()[:, None]
        x = resample(x, a.sr, sr)
        if not stereo:
            x = x[:, 0]
        i0 = int(round(off * sr))
        s0 = max(0, -i0)
        i0 = max(0, i0)
        m = min(len(x) - s0, n - i0)
        if m > 0:
            out[i0:i0 + m] += x[s0:s0 + m]
    return out


def run_analysis(project: Project, audio: dict[str, AudioData], opts: AnalysisOptions,
                 progress: Callable[[float, str], None] | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 cache_dir: str | None = None) -> AnalysisResult:
    sr = ANALYSIS_SR
    res = AnalysisResult()

    def step(frac: float, msg: str) -> None:
        if cancelled and cancelled():
            raise Cancelled()
        res.log.append(msg)
        if progress:
            progress(frac, msg)

    tracks = [t for t in project.tracks if t.id in audio]
    mains = [t for t in tracks if t.analyse and t.role == "Track"]
    stems = [t for t in tracks if t.analyse and t.role == "Stem"]
    clicks = [t for t in tracks if t.role == "Click"]
    mix_tracks = mains or stems
    if not mix_tracks:
        res.log.append("No tracks selected for analysis (role Track or Stem with 'Analyse' on).")
        return res

    step(0.02, "Preparing audio")
    y = _timeline_mix([(audio[t.id], t.offset) for t in mix_tracks], sr)

    # --- beat grid --------------------------------------------------------
    grid = project.beat_grid if project.beat_grid.confirmed else None
    if opts.grid and not (grid and grid.source == "manual"):
        if clicks:
            step(0.08, f"Building grid from click track '{clicks[0].name}'")
            yc = _timeline_mix([(audio[clicks[0].id], clicks[0].offset)], sr)
            g = beats_mod.grid_from_click(yc, sr, opts.beats_per_bar or 4)
        else:
            step(0.08, "Detecting beats and downbeats")
            g = beats_mod.detect_beats(y, sr, opts.use_deep_models, opts.beats_per_bar)
        if not g.empty:
            res.grid = g
            res.log.append(f"Grid: {g.bpm():.1f} BPM from {g.source} (confidence {g.confidence:.0%})")
            grid = g
    grid_beats = grid.beats if grid else []
    grid_downbeats = grid.downbeats if grid else []

    snap = project.analysis.snap_to_grid
    window = project.analysis.snap_window

    def snap_time(t: float) -> tuple[float, bool]:
        if snap and grid_beats:
            arr = np.asarray(grid_beats)
            j = int(np.argmin(np.abs(arr - t)))
            if abs(arr[j] - t) <= window:
                return float(arr[j]), True
        return t, False

    def on_downbeat(t: float) -> bool:
        return bool(grid_downbeats) and float(np.min(np.abs(np.asarray(grid_downbeats) - t))) < 0.02

    # --- sources: which audio each detector listens to -------------------------
    drum_src: np.ndarray | None = None       # drums only (stem) -> no HPSS needed
    melodic_srcs: list[tuple[str, np.ndarray, bool]] = []   # (name, audio, is_mix)
    harmony_src = y
    other_stems = []
    for t in stems:
        nm = t.name.lower()
        ys = _timeline_mix([(audio[t.id], t.offset)], sr)
        if any(w in nm for w in DRUM_WORDS):
            drum_src = ys if drum_src is None else drum_src + ys
        elif "bass" not in nm:
            other_stems.append((t.name, ys))
    for name, ys in other_stems:
        # only single-line parts (vocals, leads, solos) become lead lines; chord
        # parts (keys, rhythm guitar, pads) feed the chord-change detector instead
        if opts.melody and is_lead_like(ys, sr, name):
            melodic_srcs.append((name, ys, False))
        elif opts.melody:
            res.log.append(f"'{name}' plays chords: used for chord changes, not lead lines")
    if other_stems:
        harmony_src = sum(ys for _, ys in other_stems)
    wants_sep = opts.use_demucs and not stems and (opts.hits or opts.fills or opts.melody)
    if wants_sep and demucs_available():
        step(0.15, "Separating stems with Demucs (slow on CPU, cached afterwards)")
        ys2 = _timeline_mix([(audio[t.id], t.offset) for t in mix_tracks], 44100, stereo=True)
        sep = separate(ys2, 44100, cache_dir)
        if sep:
            def r(x):
                return resample(x[:, None], 44100, sr)[:, 0]
            drum_src = r(sep["drums"]) if "drums" in sep else None
            if "vocals" in sep:
                melodic_srcs.append(("Vocal", r(sep["vocals"]), False))
            if "other" in sep:
                melodic_srcs.append(("Instrument", r(sep["other"]), True))
            res.log.append("Stems separated with Demucs")
        else:
            res.log.append("Demucs unavailable or failed; using the full mix.")
    if not melodic_srcs and opts.melody_from_mix:
        melodic_srcs.append(("", y, True))
    spectra = Spectra(y, sr)

    # --- fills (before hits, so hits inside a fill can defer to it) -----------
    fills = []
    if opts.fills and grid_beats:
        res.kinds.add("fill")
        step(0.25, "Looking for drum fills")
        fb = list(grid_beats) + ([grid_beats[-1] + (grid_beats[-1] - grid_beats[-2])] if len(grid_beats) > 1 else [])
        bpb = grid.beats_per_bar if grid else 4
        fills = detect_fills(drum_src if drum_src is not None else y, sr, fb, grid_downbeats, bpb,
                             percussive=True, clean_source=drum_src is not None)
        for f in fills:
            res.suggestions.append(Suggestion(
                "fill", round(f.start, 6), f.confidence, f.reason, label=f.label,
                duration=round(f.end - f.start, 3),
                idea="strobe through the fill (Off on the landing), big hit on the downbeat"))

    def in_fill(t: float) -> bool:
        return any(f.start - 0.03 <= t < f.end - 0.03 for f in fills)

    # --- hits ---------------------------------------------------------------
    if opts.hits:
        res.kinds.add("hit")
        sources: list[tuple[str, np.ndarray, tuple[str, ...], bool]] = []
        if drum_src is not None:
            sources.append(("", drum_src, ("kick", "snare", "crash"), False))
        else:
            sources.append(("", y, ("kick", "snare", "crash"), True))
        hit_sugs = []
        for i, (name, ys, bands, perc) in enumerate(sources):
            step(0.35, f"Detecting hits{(' in ' + name) if name else ''}")
            for h in detect_hits(ys, sr, bands, source=name, percussive=perc,
                                 spectra=spectra if ys is y else None):
                t, snapped = snap_time(h.time)
                conf = h.confidence
                reason = h.reason
                if snapped:
                    reason += ", on beat"
                    conf = min(1.0, conf + 0.05)
                    if on_downbeat(t):
                        reason += " (downbeat)"
                        conf = min(1.0, conf + 0.05)
                if in_fill(t):
                    # part of a fast fill: the fill's strobe covers it, so hide it by default
                    reason += ", inside a drum fill (covered by the fill suggestion)"
                    conf *= 0.6
                idea = {"Kick": "bump / flash on the kick", "Snare": "hit / strobe flash on the snare",
                        "Crash": "big hit: full-rig flash or blinder"}.get(h.label.split()[-1], "")
                hit_sugs.append(Suggestion("hit", round(t, 6), round(conf, 3), reason,
                                           label=h.label, source_track=name, idea=idea))
        res.suggestions += _dedupe(hit_sugs, 0.04)

    # --- harmony ------------------------------------------------------------
    chord_times: list[float] = []
    if opts.harmony and grid_beats:
        res.kinds.add("harmony")
        step(0.45, "Following chord changes")
        for c in detect_chord_changes(harmony_src, sr, grid_beats, grid_downbeats,
                                      spectra=spectra if harmony_src is y else None):
            chord_times.append(c.time)
            res.suggestions.append(Suggestion("harmony", round(c.time, 6), c.confidence, c.reason,
                                              label=f"Chord → {c.chord}",
                                              idea="colour change / new palette on the chord"))

    # --- melody / lead lines --------------------------------------------------
    if opts.melody and melodic_srcs:
        res.kinds.add("melody")
        for name, ys, is_mix in melodic_srcs:
            step(0.55, f"Following the lead line{(' in ' + name) if name else ''}")
            phrases = detect_phrases(ys, sr, chord_times, harmonic=is_mix, source=name)
            for ph in phrases:
                t, _ = snap_time(ph.start)
                conf = ph.confidence
                reason = ph.reason
                if is_mix and not name:
                    # lead lines from a full mix are much less reliable than from stems
                    conf = min(conf, 0.45)
                    reason += " (estimated from the full mix: import stems or enable Demucs for accuracy)"
                idea = ph.idea
                if "vocal" in name.lower() or "vox" in name.lower():
                    idea = "follow-spot / key light on the singer for this line"
                res.suggestions.append(Suggestion("melody", round(t, 6), round(conf, 3), reason,
                                                  label=ph.label, source_track=name, idea=idea,
                                                  duration=round(ph.end - ph.start, 3),
                                                  steps=[round(x, 4) for x in ph.notes[:128]]))

    # --- sections -----------------------------------------------------------
    if opts.sections:
        res.kinds.add("section")
        step(0.6, "Finding song sections")
        bounds = None
        if opts.use_deep_models and len(mix_tracks) == 1 and mix_tracks[0].offset == 0:
            bounds = detect_sections_allin1(mix_tracks[0].path)
            if bounds:
                res.log.append("Sections from All-In-One")
        if bounds is None:
            bounds = detect_sections(y, sr, grid_beats, grid_downbeats)
        for b in bounds:
            res.suggestions.append(Suggestion("section", round(b.time, 6), b.confidence, b.reason, label=b.label))

    # --- energy -------------------------------------------------------------
    if opts.energy:
        res.kinds.add("energy")
        step(0.8, "Finding drops, builds and blackouts")
        for e in detect_energy(y, sr, grid_downbeats):
            res.suggestions.append(Suggestion("energy", round(e.time, 6), round(e.confidence, 3), e.reason,
                                              label=e.label))

    # A drum fill usually leads into a new section: the change itself happens on the
    # downbeat the fill lands on, so move section/energy changes found at the start
    # of (or just before) a fill onto its landing.
    if fills:
        bar = (grid_downbeats[1] - grid_downbeats[0]) if len(grid_downbeats) > 1 else 2.0
        for sg in res.suggestions:
            if sg.kind not in ("section", "energy") or sg.label in ("Blackout", "Return", "Build"):
                continue
            for f in fills:
                if sg.time - 0.1 <= f.end <= sg.time + 2.2 * bar and f.start >= sg.time - 0.1 - bar and \
                        abs(f.end - sg.time) > 0.05:
                    sg.time = round(f.end, 6)
                    sg.reason += ", moved to where the drum fill lands"
                    sg.confidence = round(min(1.0, sg.confidence + 0.1), 3)
                    break
    res.suggestions.sort(key=lambda s: s.time)
    step(1.0, f"Done: {len(res.suggestions)} suggestions")
    return res


def _dedupe(sugs: list[Suggestion], window: float) -> list[Suggestion]:
    sugs = sorted(sugs, key=lambda s: s.time)
    out: list[Suggestion] = []
    for s in sugs:
        if out and out[-1].kind == s.kind and s.time - out[-1].time < window:
            keep, other = (s, out[-1]) if s.confidence > out[-1].confidence else (out[-1], s)
            if other.source_track and other.source_track not in keep.reason:
                keep.reason += f" + {other.label.lower()}"
            out[-1] = keep
        else:
            out.append(s)
    return out
