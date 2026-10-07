"""Analysis in a separate process.

The UI writes a job folder (job.json: the project, the song to analyse, the options) and
starts `python -m cueforge.analysis.worker <folder>` (or `CueForge --analysis-worker
<folder>` in the packaged app). The worker loads the song's audio itself, runs the
pipeline at lower priority and reports through files in the folder:

* progress.jsonl — one JSON object per line: {"p": 0.35, "msg": "...", "t": seconds}
* result.json    — the AnalysisResult when it finished
* error.txt      — the traceback if it failed

Files instead of stdout because the packaged Windows app has no console streams. A
separate process keeps the UI responsive whatever the models do with the CPU and the GIL,
and cancelling is simply ending the process.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import asdict


def lower_priority() -> None:
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        else:
            os.nice(8)
    except Exception:
        pass
    try:
        import torch
        torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))   # leave a core for the UI and audio
    except Exception:
        pass


def write_job(folder: str, project, song_id: str, opts, cache_dir: str | None) -> str:
    """Write job.json for `song_id` of `project` (track paths made absolute)."""
    os.makedirs(folder, exist_ok=True)
    d = project.to_dict()
    for s in d["songs"]:
        for t in s.get("tracks", []):
            t["path"] = os.path.abspath(t["path"]) if t.get("path") else t.get("path")
    job = {"project": d, "song_id": song_id, "options": asdict(opts), "cache_dir": cache_dir}
    path = os.path.join(folder, "job.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(job, f)
    return path


def result_to_dict(res) -> dict:
    return {"grid": asdict(res.grid) if res.grid else None,
            "suggestions": [asdict(s) for s in res.suggestions],
            "kinds": sorted(res.kinds), "log": list(res.log)}


def result_from_dict(d: dict):
    from ..core.model import BeatGrid, Suggestion, _from_dict
    from .pipeline import AnalysisResult
    return AnalysisResult(grid=_from_dict(BeatGrid, d["grid"]) if d.get("grid") else None,
                          suggestions=[_from_dict(Suggestion, s) for s in d.get("suggestions", [])],
                          kinds=set(d.get("kinds", [])), log=list(d.get("log", [])))


def run(folder: str) -> int:
    t0 = time.monotonic()
    prog_path = os.path.join(folder, "progress.jsonl")

    def report(p: float, msg: str) -> None:
        line = json.dumps({"p": round(float(p), 4), "msg": msg, "t": round(time.monotonic() - t0, 2)}) + "\n"
        with open(prog_path, "ab") as f:            # binary: same bytes on every OS
            f.write(line.encode("utf-8"))

    # the packaged Windows app has no console streams; libraries that print must not fail
    log = open(os.path.join(folder, "worker.log"), "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or getattr(sys, "frozen", False):
        sys.stdout = log
    if sys.stderr is None or getattr(sys, "frozen", False):
        sys.stderr = log
    try:
        lower_priority()
        report(0.0, "Starting analysis")
        with open(os.path.join(folder, "job.json"), encoding="utf-8") as f:
            job = json.load(f)
        from ..audio.loader import load_audio
        from ..core.model import Project
        from .pipeline import AnalysisOptions, run_analysis
        project = Project.from_dict(job["project"])
        project.select_song(job["song_id"])
        opts = AnalysisOptions(**{k: v for k, v in job["options"].items()
                                  if k in AnalysisOptions.__dataclass_fields__})
        audio = {}
        spoken = opts.sections and opts.spoken_cues
        want = [t for t in project.tracks if (t.analyse and t.role in ("Track", "Stem")) or t.role == "Click"
                or (spoken and t.role == "Cue/Guide")]
        for i, t in enumerate(want):
            report(0.0, f"Loading audio: {t.name}")
            try:
                audio[t.id] = load_audio(t.path)
            except Exception as exc:
                report(0.0, f"Could not load {t.name}: {exc}")
        if not any(t.id in audio for t in want if t.role in ("Track", "Stem", "Cue/Guide")):
            raise RuntimeError("None of this song's audio files could be loaded (moved or deleted?): "
                               + ", ".join(os.path.basename(t.path or "?") for t in want))
        res = run_analysis(project, audio, opts, progress=report, cache_dir=job.get("cache_dir"))
        tmp = os.path.join(folder, "result.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result_to_dict(res), f)
        os.replace(tmp, os.path.join(folder, "result.json"))
        return 0
    except Exception:
        with open(os.path.join(folder, "error.txt"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        return 1


def worker_command(folder: str) -> tuple[str, list[str], dict]:
    """(program, arguments, extra environment) that runs the worker on `folder`."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--analysis-worker", folder], {}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pp = os.environ.get("PYTHONPATH", "")
    return sys.executable, ["-m", "cueforge.analysis.worker", folder], \
        {"PYTHONPATH": root + (os.pathsep + pp if pp else "")}


if __name__ == "__main__":
    from ..addons import activate
    activate()
    sys.exit(run(sys.argv[1]))
