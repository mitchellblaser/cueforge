"""Install / remove the optional AI models from inside CueForge (no pip on the command line)."""
from __future__ import annotations

import os
import sys
import tempfile

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QVBoxLayout)

from .. import addons
from . import theme


class AIModelsDialog(QDialog):
    def __init__(self, settings=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI models")
        self.setMinimumWidth(620)
        self.settings = settings
        self.proc: QProcess | None = None
        self.log_path = ""
        self._log_pos = 0
        self._phase = ""
        self._queue: list[tuple] = []
        lay = QVBoxLayout(self)
        intro = QLabel("CueForge works without these. They make beat grids and stem separation much better on "
                       "difficult material (live recordings, rubato, dense mixes), at the cost of a large download "
                       "and slower analysis. Everything installs into CueForge's own folder and can be removed "
                       "again here.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.boxes: dict[str, QCheckBox] = {}
        st = addons.status()
        for c in addons.COMPONENTS:
            row = QVBoxLayout()
            cb = QCheckBox(f"{c.label}  ({c.size})" + ("   ✓ installed" if st[c.key] else ""))
            cb.setChecked(True)
            self.boxes[c.key] = cb
            d = QLabel(c.what)
            d.setWordWrap(True)
            d.setStyleSheet(f"color: {theme.FG_DIM}; margin-left: 22px;")
            row.addWidget(cb)
            row.addWidget(d)
            lay.addLayout(row)
        torch_note = QLabel(f"Both need PyTorch: {addons.TORCH_SIZE}.")
        torch_note.setWordWrap(True)
        torch_note.setStyleSheet(f"color: {theme.FG_DIM};")
        lay.addWidget(torch_note)
        self.gpu = QCheckBox("Use my NVIDIA graphics card (CUDA build of PyTorch, much bigger download)")
        self.gpu.setVisible(sys.platform != "darwin")
        lay.addWidget(self.gpu)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.state = QLabel("")
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFixedHeight(150)
        self.log.setVisible(False)
        lay.addWidget(self.log)
        btns = QHBoxLayout()
        self.install_btn = QPushButton("Install")
        self.install_btn.setDefault(True)
        self.install_btn.clicked.connect(self.install)
        self.remove_btn = QPushButton("Remove AI models")
        self.remove_btn.clicked.connect(self.remove)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.remove_btn)
        btns.addStretch()
        btns.addWidget(self.install_btn)
        btns.addWidget(self.close_btn)
        lay.addLayout(btns)
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tail)
        self._refresh()

    # ---------------------------------------------------------------- state
    def _refresh(self) -> None:
        st = addons.status()
        busy = self.proc is not None
        self.remove_btn.setEnabled(any(st.values()) and not busy)
        self.install_btn.setEnabled(not busy)
        self.install_btn.setText("Install / update" if any(st.values()) else "Install")
        for c in addons.COMPONENTS:
            self.boxes[c.key].setEnabled(not busy)
            self.boxes[c.key].setText(f"{c.label}  ({c.size})" + ("   ✓ installed" if st[c.key] else ""))
        if not busy:
            have = [c.label for c in addons.COMPONENTS if st[c.key]]
            self.state.setText("Installed: " + ", ".join(have) if have else "Not installed")

    def keys(self) -> list[str]:
        return [k for k, cb in self.boxes.items() if cb.isChecked()]

    # ---------------------------------------------------------------- install
    def install(self) -> None:
        keys = self.keys()
        if not keys:
            return
        self.log_path = os.path.join(tempfile.mkdtemp(prefix="cueforge-ai-"), "install.log")
        self._queue = [("Downloading and installing (this can take a while)…",
                        addons.install_command(keys, self.log_path, self.gpu.isChecked())),
                       ("Downloading the model weights…", addons.prefetch_command(keys, self.log_path))]
        self.log.clear()
        self.log.setVisible(True)
        self.bar.setVisible(True)
        self._next()

    def _next(self) -> None:
        if not self._queue:
            self._finished_all()
            return
        label, (prog, args, env) = self._queue.pop(0)
        self._phase = label
        self.state.setText(label)
        self.proc = QProcess(self)
        pe = QProcessEnvironment.systemEnvironment()
        for k, v in env.items():
            pe.insert(k, v)
        pe.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(pe)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyRead.connect(lambda: self._append(bytes(self.proc.readAll()).decode("utf-8", "replace")))
        self.proc.finished.connect(self._step_done)
        self.proc.errorOccurred.connect(self._error)
        self.proc.start(prog, args)
        self._timer.start()
        self._refresh()

    def _append(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self.log.appendPlainText(line)
                low = line.lower()
                if low.startswith(("collecting", "downloading", "installing", "successfully", "downloading")):
                    self.state.setText(f"{self._phase}\n{line.strip()[:120]}")

    def _tail(self) -> None:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                f.seek(self._log_pos)
                chunk = f.read()
                self._log_pos = f.tell()
        except OSError:
            return
        if chunk:
            self._append(chunk)

    def _error(self, err) -> None:
        if err == QProcess.FailedToStart:
            self._tail()
            self._fail(f"Could not start the installer: {self.proc.errorString() if self.proc else ''}")

    def _step_done(self, code, _status) -> None:
        self._tail()
        installing = "installing" in self._phase.lower()
        self.proc = None
        if code != 0 and installing:
            self._fail("The installation failed. The log above says why — most often no internet connection, "
                       "or no PyTorch build for this computer.")
            return
        self._next()                       # a failed weight download is fine: they download on first use

    def _fail(self, msg: str) -> None:
        self._timer.stop()
        self._queue = []
        self.proc = None
        self.bar.setVisible(False)
        self.state.setText(f"<span style='color:#ef5350'>{msg}</span>")
        self._refresh()

    def _finished_all(self) -> None:
        self._timer.stop()
        self.bar.setVisible(False)
        addons.activate()
        st = addons.status()
        ok = [c.label for c in addons.COMPONENTS if st[c.key] and c.key in self.keys()]
        self._refresh()
        if ok:
            self.state.setText("Ready: " + ", ".join(ok) + ". They are used the next time you analyse "
                               "(no restart needed).")
            if self.settings is not None:
                self.settings.set("ai_prompt_done", True)
        else:
            self.state.setText("<span style='color:#ef5350'>The packages installed but could not be loaded. "
                               "Restart CueForge and check again.</span>")

    # ---------------------------------------------------------------- remove
    def remove(self) -> None:
        if QMessageBox.question(self, "Remove AI models",
                                "Remove the AI models and PyTorch from CueForge's folder? Analysis falls back to "
                                "the built-in methods.") != QMessageBox.Yes:
            return
        self.state.setText(addons.uninstall() + ". Restart CueForge to finish unloading them.")
        self._refresh()

    def reject(self) -> None:
        if self.proc is not None:
            if QMessageBox.question(self, "AI models", "Stop the installation?") != QMessageBox.Yes:
                return
            self.proc.kill()
            self.proc = None
        super().reject()


def first_run_prompt(settings, parent=None) -> None:
    """Offer the AI models once, on first start (or until the user decides)."""
    if settings.get("ai_prompt_done") or addons.any_installed() or os.environ.get("CUEFORGE_NO_FIRST_RUN"):
        return
    box = QMessageBox(parent)
    box.setWindowTitle("Advanced AI models")
    box.setIcon(QMessageBox.Question)
    box.setText("<b>Install the advanced AI models?</b>")
    box.setInformativeText(
        "Beat This! (beats and downbeats) and Demucs (stem separation) make the analysis noticeably better on "
        "live recordings and dense mixes. It is a one-time download of about 600 MB; CueForge installs it "
        "itself.\n\nYou can do this later from AI ▸ AI models….")
    yes = box.addButton("Install now…", QMessageBox.AcceptRole)
    later = box.addButton("Later", QMessageBox.RejectRole)
    never = box.addButton("Don't ask again", QMessageBox.DestructiveRole)
    box.setDefaultButton(yes)
    box.exec()
    clicked = box.clickedButton()
    if clicked is never:
        settings.set("ai_prompt_done", True)
    elif clicked is yes:
        dlg = AIModelsDialog(settings, parent)
        dlg.exec()
    del later
