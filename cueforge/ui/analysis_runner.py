"""Runs one analysis job in a child process and reports smooth progress.

The pipeline reports a handful of checkpoints (0.08 beats, 0.15 Demucs, 0.25 fills …) and
some stages take far longer than others, so the raw numbers jump. ProgressModel spreads
the bar over time instead: each stage is expected to take (learned seconds per minute of
audio) × (song length), the bar moves with the clock inside the current stage, and it never
passes the next real checkpoint. The time each stage actually took is remembered, so the
estimate fits this computer after the first run or two.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

# seconds of work per minute of audio, before anything is learned (typical laptop CPU)
DEFAULT_RATES = {
    "load": 1.0, "0.02": 0.5, "0.08": 3.0, "0.08d": 25.0, "0.15": 60.0, "0.25": 2.5, "0.35": 3.0,
    "0.45": 2.5, "0.55": 4.0, "0.60": 3.0, "0.60d": 30.0, "0.80": 1.0,
}
STAGES = [0.0, 0.02, 0.08, 0.15, 0.25, 0.35, 0.45, 0.55, 0.60, 0.80, 1.0]


def stage_key(p: float, deep: bool) -> str:
    if p <= 0.0:
        return "load"
    k = f"{p:.2f}"
    return k + "d" if deep and k in ("0.08", "0.60") else k


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class ProgressModel:
    """Turns sparse checkpoints into a steadily moving fraction and a time estimate."""

    def __init__(self, audio_minutes: float, deep: bool, demucs: bool, learned: dict | None = None,
                 skip: set[float] | None = None):
        self.minutes = max(0.25, audio_minutes)
        self.deep = deep
        self.learned = dict(learned or {})
        self.expect: dict[float, float] = {}
        for p in STAGES[:-1]:
            if (p == 0.15 and not demucs) or p in (skip or ()):
                continue
            key = stage_key(p, deep)
            self.expect[p] = self.rate(key) * self.minutes
        self.total = sum(self.expect.values()) or 1.0
        self.skip = set(skip or ())
        self.cur = 0.0               # checkpoint of the running stage
        self.cur_since = 0.0         # seconds (job clock) when it started
        self.took: dict[str, float] = {}
        self.shown = 0.0
        self._last_t: float | None = None

    def rate(self, key: str) -> float:
        return float(self.learned.get(key, DEFAULT_RATES.get(key, 2.0)))

    def feed(self, p: float, t: float) -> None:
        """A checkpoint `p` was reported at job time `t` (seconds since the worker started)."""
        p = max(STAGES[0], min(1.0, p))
        if p == self.cur:
            return
        if p > self.cur:
            key = stage_key(self.cur, self.deep)
            self.took[key] = self.took.get(key, 0.0) + max(0.0, t - self.cur_since)
            self.cur, self.cur_since = p, t

    def fraction(self, t: float) -> float:
        if self.cur >= 1.0:
            return 1.0
        done = sum(v for k, v in self.expect.items() if k < self.cur)
        exp = self.expect.get(self.cur, 0.0)
        inside = max(0.0, t - self.cur_since)
        if exp <= 0:
            part = 0.0
        elif inside <= exp:                 # on schedule: move with the clock
            part = 0.9 * inside
        else:                               # overrunning: keep creeping, never finish the stage
            part = exp * (0.9 + 0.09 * (1 - exp / inside))
        target = min(0.995, (done + part) / self.total)
        # ease towards the target (a stage that finishes early would otherwise make a jump)
        if self._last_t is None or t <= self._last_t:
            eased = self.shown if self._last_t is not None else target
        else:
            a = 1.0 - math.exp(-(t - self._last_t) / 1.2)
            eased = self.shown + (target - self.shown) * a
        self._last_t = t
        self.shown = max(self.shown, eased)
        return self.shown

    def remaining(self, t: float) -> float:
        exp = self.expect.get(self.cur, 0.0)
        left_here = max(exp - (t - self.cur_since), exp * 0.1)
        return left_here + sum(v for k, v in self.expect.items() if k > self.cur)

    def learn(self) -> dict:
        """Updated seconds-per-audio-minute table after a finished run."""
        out = dict(self.learned)
        for key, sec in self.took.items():
            r = sec / self.minutes
            old = out.get(key)
            out[key] = round(r if old is None else 0.5 * old + 0.5 * r, 3)
        return out


class AnalysisRunner(QObject):
    """One song's analysis in a worker process."""
    progress = Signal(float, str)
    finished = Signal(object)        # AnalysisResult
    failed = Signal(str)

    def __init__(self, folder: str, model: ProgressModel, parent=None) -> None:
        super().__init__(parent)
        self.folder = folder
        self.model = model
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyRead.connect(self._drain)
        self.proc.finished.connect(self._exited)
        self.proc.errorOccurred.connect(self._error)
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._poll)
        self._offset = 0
        self._msg = "Starting analysis"
        self._job_t = 0.0             # worker clock at the last report
        self._wall_at = time.monotonic()
        self._started = time.monotonic()
        self._out = b""
        self._done = False
        self.cancelled = False

    def start(self) -> None:
        from ..analysis.worker import worker_command
        prog, args, env = worker_command(self.folder)
        pe = QProcessEnvironment.systemEnvironment()
        for k, v in env.items():
            pe.insert(k, v)
        pe.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(pe)
        self._started = time.monotonic()
        self.proc.start(prog, args)
        self._timer.start()

    def cancel(self) -> None:
        self.cancelled = True
        self._timer.stop()
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)
        self._done = True
        shutil.rmtree(self.folder, ignore_errors=True)

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def _drain(self) -> None:
        self._out = (self._out + bytes(self.proc.readAll()))[-20000:]

    def _now(self) -> float:
        """Job clock: last reported worker time plus wall time since that report."""
        return self._job_t + (time.monotonic() - self._wall_at)

    def _poll(self) -> None:
        path = os.path.join(self.folder, "progress.jsonl")
        try:
            with open(path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1                 # only consume whole lines
            self._offset += end
            lines = chunk[:end].decode("utf-8", "replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            self._job_t, self._wall_at = float(d.get("t", 0.0)), time.monotonic()
            self.model.feed(float(d.get("p", 0.0)), self._job_t)
            self._msg = d.get("msg", self._msg)
        if not self._done:
            now = self._now()
            frac = self.model.fraction(now)
            left = self.model.remaining(now)
            txt = f"{self._msg} · {fmt_time(self.elapsed())}"
            if self.elapsed() > 5:
                txt += f" · about {fmt_time(left)} left" if left >= 60 else " · less than a minute left"
            self.progress.emit(frac, txt)

    def _error(self, err) -> None:
        if err == QProcess.FailedToStart and not self._done:
            self._done = True
            self._timer.stop()
            shutil.rmtree(self.folder, ignore_errors=True)
            self.failed.emit(f"Could not start the analysis process: {self.proc.errorString()}")

    def _exited(self, code, status) -> None:
        if self._done:
            return
        self._poll()
        self._done = True
        self._timer.stop()
        res_path = os.path.join(self.folder, "result.json")
        err_path = os.path.join(self.folder, "error.txt")
        try:
            if self.cancelled:
                self.failed.emit("Cancelled")
            elif os.path.exists(res_path):
                from ..analysis.worker import result_from_dict
                try:
                    with open(res_path, encoding="utf-8") as f:
                        res = result_from_dict(json.load(f))
                except Exception:
                    import traceback
                    self.failed.emit("Could not read the analysis result:\n" + traceback.format_exc())
                    return
                self.model.feed(1.0, self._now())
                self.finished.emit(res)
            else:
                msg = ""
                if os.path.exists(err_path):
                    with open(err_path, encoding="utf-8", errors="replace") as f:
                        msg = f.read().strip()
                tail = self._out.decode("utf-8", "replace")[-3000:]
                self.failed.emit(msg or f"The analysis process stopped (code {code}).\n\n{tail}")
        finally:
            shutil.rmtree(self.folder, ignore_errors=True)
