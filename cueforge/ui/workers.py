"""Run slow jobs (decoding, analysis) off the UI thread."""
from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot


class Job(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., Any], *args, **kwargs) -> None:
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.cancel_requested = False

    def run(self) -> None:
        try:
            result = self.fn(*self.args, progress=self._progress, cancelled=self._cancelled, **self.kwargs)
        except Exception as exc:  # report everything to the UI
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}" if not str(exc) == "" else traceback.format_exc())
            return
        self.finished.emit(result)

    def _progress(self, frac: float, msg: str) -> None:
        self.progress.emit(frac, msg)

    def _cancelled(self) -> bool:
        return self.cancel_requested


class Relay(QObject):
    """Lives in the UI thread and re-emits a Job's signals there, so plain
    lambdas connected to it always run on the UI thread."""
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)

    @Slot(float, str)
    def on_progress(self, f, m):
        self.progress.emit(f, m)

    @Slot(object)
    def on_finished(self, r):
        self.finished.emit(r)

    @Slot(str)
    def on_failed(self, e):
        self.failed.emit(e)


_running: list[tuple[QThread, Job, Relay]] = []


def start_job(job: Job) -> Relay:
    """Start `job` on its own thread; connect to the returned Relay's signals."""
    relay = Relay()
    job.progress.connect(relay.on_progress)
    job.finished.connect(relay.on_finished)
    job.failed.connect(relay.on_failed)
    relay.job = job
    thread = QThread()
    job.moveToThread(thread)
    thread.started.connect(job.run)

    def cleanup(*_):
        thread.quit()

    job.finished.connect(cleanup)
    job.failed.connect(cleanup)

    # keep references until the thread has fully stopped (pruned lazily here)
    _running[:] = [r for r in _running if not r[0].isFinished()]
    _running.append((thread, job, relay))
    thread.start()
    return relay


def wait_all(timeout_ms: int = 3000) -> None:
    for t, j, _ in list(_running):
        j.cancel_requested = True
        t.quit()
        t.wait(timeout_ms)
