"""Score the analysis against the corpus ground truth.

    python -m tests.evaluate            # full mix only
    python -m tests.evaluate --stems    # also with stems imported as Stem tracks
"""
from __future__ import annotations

import sys
import time

import numpy as np

from cueforge.analysis.pipeline import AnalysisOptions, run_analysis
from cueforge.audio.loader import ENGINE_SR, AudioData, PeakPyramid, resample
from cueforge.core.model import Project, Track
from tests import corpus


def f_measure(ref, est, tol):
    ref, est = list(ref), list(est)
    if not ref and not est:
        return 1.0, 1.0, 1.0
    if not ref or not est:
        return 0.0, 0.0, 0.0
    used = set()
    tp = 0
    for r in ref:
        best = None
        for j, e in enumerate(est):
            if j not in used and abs(e - r) <= tol and (best is None or abs(e - r) < abs(est[best] - r)):
                best = j
        if best is not None:
            used.add(best)
            tp += 1
    p, r = tp / len(est), tp / len(ref)
    return (2 * p * r / (p + r) if p + r else 0.0), p, r


def beat_scores(ref, est):
    import mir_eval
    ref, est = np.asarray(ref), np.asarray(est)
    ref = mir_eval.beat.trim_beats(ref, 0.0) if len(ref) else ref
    est = mir_eval.beat.trim_beats(est, 0.0) if len(est) else est
    f = mir_eval.beat.f_measure(ref, est, 0.07)
    _, _, cmlt, amlt = mir_eval.beat.continuity(ref, est)
    return f, cmlt, amlt


def hit_truth(truth) -> list[float]:
    """Kicks, snares and crashes outside fills, de-duplicated within 30 ms."""
    t = sorted(truth["kicks"] + truth["snares"] + truth.get("crashes", []))
    t = [x for x in t if not any(a - 0.05 <= x < b - 0.05 for a, b in truth["fills"])]
    out = []
    for x in t:
        if not out or x - out[-1] > 0.03:
            out.append(x)
    return out


def interval_hits(ref_intervals, est_times, tol=0.3):
    """Fill detection: an estimate is correct if it starts within a reference fill
    (with tolerance); recall counts reference fills that got at least one estimate."""
    if not ref_intervals and not est_times:
        return 1.0, 1.0, 1.0
    if not est_times:
        return 0.0, 1.0, 0.0
    hit_ref = sum(1 for a, b in ref_intervals if any(a - tol <= e <= b for e in est_times))
    good = sum(1 for e in est_times if any(a - tol <= e <= b for a, b in ref_intervals))
    p = good / len(est_times)
    r = hit_ref / len(ref_intervals) if ref_intervals else 1.0
    return (2 * p * r / (p + r) if p + r else 0.0), p, r


def audio_data(mono_or_stereo: np.ndarray, sr: int) -> AudioData:
    x = mono_or_stereo if mono_or_stereo.ndim == 2 else np.stack([mono_or_stereo] * 2, 1)
    x = resample(x.astype(np.float32), sr, ENGINE_SR)
    return AudioData("mem", ENGINE_SR, x, PeakPyramid.build(x.mean(1), ENGINE_SR))


def analyse_song(spec, use_stems=False, opts=None):
    mix, stems, truth = corpus.render(spec)
    p = Project()
    audio = {}
    if use_stems:
        for name, y in stems.items():
            t = Track(name, f"{name}.wav", role="Stem")
            p.tracks.append(t)
            audio[t.id] = audio_data(y, corpus.SR)
    else:
        t = Track(spec.name, f"{spec.name}.wav")
        p.tracks.append(t)
        audio[t.id] = audio_data(mix, corpus.SR)
    opts = opts or AnalysisOptions(use_deep_models=False, beats_per_bar=0)
    t0 = time.time()
    res = run_analysis(p, audio, opts)
    return res, truth, time.time() - t0


def score(res, truth) -> dict:
    out = {}
    g = res.grid
    if g and g.beats:
        f, cmlt, amlt = beat_scores(truth["beats"], g.beats)
        out["beatF"], out["beatAMLt"] = f, amlt
        out["downF"] = f_measure(truth["downbeats"], g.downbeats, 0.07)[0]
        out["meter"] = g.beats_per_bar
    else:
        out.update(beatF=0, beatAMLt=0, downF=0, meter=0)
    kinds = {}
    for s in res.suggestions:
        kinds.setdefault(s.kind, []).append(s)
    th = {"hit": 0.75, "fill": 0.5, "melody": 0.5, "harmony": 0.5, "section": 0.5, "energy": 0.6}
    vis = {k: [s for s in v if s.confidence >= th.get(k, 0.5)] for k, v in kinds.items()}
    fills_ref = truth["fills"]
    # estimates inside drum fills belong to the fill, not to the hit metric
    hits = [s.time for s in vis.get("hit", []) if not any(a - 0.05 <= s.time < b - 0.05 for a, b in fills_ref)]
    out["hitF"] = f_measure(hit_truth(truth), hits, 0.05)
    out["hitP"] = out["hitF"][1]
    out["hitF"] = out["hitF"][0]
    fills = [s.time for s in vis.get("fill", [])]
    out["fillF"], out["fillP"], out["fillR"] = interval_hits(truth["fills"], fills)
    phr = [s.time for s in vis.get("melody", []) if "phrase" in s.label.lower() or "enters" in s.label.lower()]
    out["phraseF"] = f_measure(truth["lead_phrases"], phr, 0.15)[0]
    chords = [s.time for s in vis.get("harmony", [])]
    out["chordF"] = f_measure(truth["chord_changes"], chords, 0.15)[0]
    sec = [s.time for s in vis.get("section", []) + vis.get("energy", [])]
    out["sectionR"] = f_measure([t for t, _ in truth["sections"][1:]], sec, 0.5)[2]
    out["n_sugs"] = len(res.suggestions)
    return out


COLS = ["beatF", "beatAMLt", "downF", "meter", "hitF", "fillF", "fillP", "fillR", "phraseF", "chordF", "sectionR"]


def main(argv):
    use_stems = "--stems" in argv
    names = [a for a in argv if not a.startswith("-")]
    rows = []
    print(f"{'song':14s}" + "".join(f"{c:>9s}" for c in COLS) + "    secs")
    for spec in corpus.corpus():
        if names and spec.name not in names:
            continue
        res, truth, dt = analyse_song(spec, use_stems)
        sc = score(res, truth)
        rows.append(sc)
        print(f"{spec.name:14s}" + "".join(f"{sc[c]:9.2f}" if c != "meter" else f"{sc[c]:9d}" for c in COLS)
              + f"   {dt:5.1f}")
    if rows:
        print(f"{'MEAN':14s}" + "".join(f"{np.mean([r[c] for r in rows]):9.2f}" for c in COLS))


if __name__ == "__main__":
    main(sys.argv[1:])
