"""Main window."""
from __future__ import annotations

import bisect
import os

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QTabWidget, QToolBar, QWidget, QSizePolicy)

from .. import __version__
from ..audio.loader import AUDIO_EXTENSIONS
from ..core import editing
from ..core.project_io import EXTENSION
from ..core.timecode import seconds_to_tc
from . import theme
from .dialogs import (AnalysisDialog, AudioDeviceDialog, CueDialog, LTCDialog, MA3ExportDialog,
                      ProjectSettingsDialog, ShortcutsDialog, TempoDialog)
from .mixer import MixerPanel
from .panels import CueTable, LanePanel, SuggestionPanel
from .session import Session
from .timeline import TimelinePanel

SPEEDS = [0.5, 0.75, 1.0]
RESERVED_KEYS = set("axsgiolcbfzp")


class MainWindow(QMainWindow):
    def __init__(self, session: Session | None = None) -> None:
        super().__init__()
        self.s = session or Session()
        self.resize(1500, 900)
        self.setAcceptDrops(True)
        self.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowTabbedDocks)

        self.timeline = TimelinePanel(self.s)
        self.canvas = self.timeline.canvas
        self.setCentralWidget(self.timeline)
        self.canvas.edit_cue_requested.connect(self.edit_cue)

        # right dock: suggestions / cues / lanes
        self.suggestions = SuggestionPanel(self.s)
        self.suggestions.analyse_requested.connect(self.analyse)
        self.suggestions.tempo_requested.connect(self.set_tempo)
        self.suggestions.seek_requested.connect(self._seek_show)
        self.cue_table = CueTable(self.s)
        self.cue_table.seek_requested.connect(self._seek_show)
        self.lanes = LanePanel(self.s)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.suggestions, "AI Suggestions")
        self.tabs.addTab(self.cue_table, "Cue list")
        self.tabs.addTab(self.lanes, "Lanes")
        right = QDockWidget("Inspector", self)
        right.setObjectName("inspector")
        right.setWidget(self.tabs)
        right.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        right.setMinimumWidth(380)
        self.addDockWidget(Qt.RightDockWidgetArea, right)

        self.mixer = MixerPanel(self.s)
        bottom = QDockWidget("Mixer", self)
        bottom.setObjectName("mixer")
        bottom.setWidget(self.mixer)
        bottom.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.BottomDockWidgetArea, bottom)

        self._build_toolbar()
        self._build_menus()
        self._build_statusbar()
        self._tap_shortcuts: list[QShortcut] = []
        self._rebuild_tap_keys()

        s = self.s
        s.lanes_changed.connect(self._rebuild_tap_keys)
        s.project_replaced.connect(self._project_replaced)
        s.dirty_changed.connect(lambda _: self._title())
        s.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        s.busy.connect(self._busy)
        s.progress.connect(self._progress)
        s.analysis_done.connect(self._analysis_done)
        s.cues_changed.connect(self._refresh_actions)
        s.selection_changed.connect(self._refresh_actions)
        s.mixer_changed.connect(self._sync_toggles)
        s.tracks_changed.connect(self._tracks_changed)

        self.clock = QTimer(self)
        self.clock.setInterval(33)
        self.clock.timeout.connect(self._clock)
        self.clock.start()
        self.autosave = QTimer(self)
        self.autosave.setInterval(120_000)
        self.autosave.timeout.connect(self._autosave)
        self.autosave.start()
        self._project_replaced()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QAbstractSpinBox, QLineEdit, QPlainTextEdit
        if ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Tab, Qt.Key_Backtab) \
                and isinstance(obj, QWidget) and obj.window() is self:
            if not isinstance(obj, (QLineEdit, QPlainTextEdit, QAbstractSpinBox)):
                back = ev.key() == Qt.Key_Backtab or bool(ev.modifiers() & Qt.ShiftModifier)
                self.jump_suggestion(-1 if back else 1)
                return True
        return False

    # ================================================================ building
    def _act(self, text: str, slot, shortcut=None, checkable=False, tip: str = "") -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.WindowShortcut)
            a.setAutoRepeat(False)
        a.setCheckable(checkable)
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda *_: slot())
        self.addAction(a)
        return a

    def _build_toolbar(self) -> None:
        tb = QToolBar("Transport")
        tb.setObjectName("transport")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)
        self.a_start = self._act("⏮", self.go_start, "Home", tip="Go to start (Home / Enter)")
        self.a_play = self._act("▶  Play", self.s.engine.toggle, "Space", tip="Play / pause (Space)")
        tb.addAction(self.a_start)
        tb.addAction(self.a_play)
        self.tc_label = QLabel("00:00:00:00")
        f = QFont("Menlo, Consolas, DejaVu Sans Mono, monospace")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(18)
        f.setBold(True)
        self.tc_label.setFont(f)
        self.tc_label.setStyleSheet(f"color: {theme.ACCENT}; padding: 0 12px;")
        self.tc_label.setMinimumWidth(190)
        tb.addWidget(self.tc_label)
        self.bar_label = QLabel("")
        self.bar_label.setStyleSheet(f"color: {theme.FG_DIM}; padding-right: 10px;")
        self.bar_label.setMinimumWidth(110)
        tb.addWidget(self.bar_label)
        self.speed = QComboBox()
        for sp in SPEEDS:
            self.speed.addItem(f"{sp:g}×", sp)
        self.speed.setCurrentIndex(SPEEDS.index(1.0))
        self.speed.setToolTip("Playback speed ([ and ])")
        self.speed.currentIndexChanged.connect(lambda i: setattr(self.s.engine, "speed", SPEEDS[i]))
        tb.addWidget(self.speed)
        tb.addSeparator()
        self.a_snap = self._act("Snap", self._set_snap, "S", True, "Snap new/moved cues to beats (S)")
        self.a_loop = self._act("Loop", self._set_loop, "L", True, "Loop region on/off (L). Shift-drag in ruler to set.")
        self.a_click = self._act("Click", lambda v: self.s.update_mixer(click_enabled=v), "C", True,
                                 "Click from the beat grid (C)")
        self.a_blips = self._act("Blips", lambda v: self.s.update_mixer(blips_enabled=v), "B", True,
                                 "Tick on every cue (B)")
        self.a_follow = self._act("Follow", self._set_follow, "F", True, "Follow playhead (F)")
        for a in (self.a_snap, self.a_loop, self.a_click, self.a_blips, self.a_follow):
            tb.addAction(a)
        tb.addSeparator()
        self.a_zoom_in = self._act("＋", lambda: self.canvas.zoom(1.5), "+", tip="Zoom in")
        self.a_zoom_out = self._act("－", lambda: self.canvas.zoom(1 / 1.5), "-", tip="Zoom out")
        self.a_fit = self._act("Fit", self.canvas.zoom_fit, "Z", tip="Zoom to fit (Z)")
        for a in (self.a_zoom_in, self.a_zoom_out, self.a_fit):
            tb.addAction(a)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.analyse_btn = QPushButton("✦ Analyse")
        self.analyse_btn.setStyleSheet(f"font-weight: bold; border-color: {theme.ACCENT};")
        self.analyse_btn.clicked.connect(self.analyse)
        tb.addWidget(self.analyse_btn)
        self.a_snap.setChecked(self.s.snap)
        self.a_follow.setChecked(True)
        # extra zoom keys
        self._act("Zoom in", lambda: self.canvas.zoom(1.5), "=")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("&File")
        f.addAction(self._act("New project", self.new_project, QKeySequence.New))
        f.addAction(self._act("Open project…", self.open_project, QKeySequence.Open))
        self.recent_menu = f.addMenu("Open recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        f.addAction(self._act("Save", self.save, QKeySequence.Save))
        f.addAction(self._act("Save as…", self.save_as, QKeySequence.SaveAs))
        f.addSeparator()
        f.addAction(self._act("Import audio…", self.import_audio, "Ctrl+I"))
        f.addSeparator()
        ex = f.addMenu("Export")
        ex.addAction(self._act("grandMA3…", self.export_ma3, "Ctrl+E"))
        ex.addAction(self._act("CSV cue list…", self.export_csv))
        ex.addAction(self._act("LTC timecode audio…", self.export_ltc))
        f.addSeparator()
        f.addAction(self._act("Project settings (frame rate, TC offset)…", self.project_settings))
        f.addAction(self._act("Audio output…", self.audio_device))
        f.addSeparator()
        f.addAction(self._act("Quit", self.close, QKeySequence.Quit))

        e = mb.addMenu("&Edit")
        self.a_undo = self._act("Undo", self._undo, QKeySequence.Undo)
        self.a_redo = self._act("Redo", self._redo, QKeySequence.Redo)
        self._act("Redo", self._redo, "Ctrl+Y")
        e.addAction(self.a_undo)
        e.addAction(self.a_redo)
        e.addSeparator()
        e.addAction(self._act("Select all cues", self.select_all, QKeySequence.SelectAll))
        e.addAction(self._act("Clear selection", lambda: self.s.select(), "Esc"))
        e.addAction(self._act("Delete selected", self.s.delete_selected, QKeySequence.Delete))
        self._act("Delete selected", self.s.delete_selected, "Backspace")
        e.addAction(self._act("Snap selected to grid", self.s.snap_selected, "G"))
        e.addAction(self._act("Edit selected cue…", self._edit_selected, "Ctrl+Return"))
        e.addSeparator()
        self._act("Nudge left", lambda: self._arrow(-1, False), "Left")
        self._act("Nudge right", lambda: self._arrow(1, False), "Right")
        self._act("Nudge left beat", lambda: self._arrow(-1, True), "Shift+Left")
        self._act("Nudge right beat", lambda: self._arrow(1, True), "Shift+Right")
        e.addAction(self._act("Add lane", lambda: self.s.add_lane()))

        a = mb.addMenu("&AI")
        a.addAction(self._act("Analyse audio…", self.analyse, "Ctrl+R"))
        a.addAction(self._act("Cancel analysis", self.s.cancel_analysis))
        a.addSeparator()
        # Tab / Shift+Tab are handled in eventFilter so focus navigation never eats them
        nxt = QAction("Next suggestion\tTab", self)
        nxt.triggered.connect(lambda: self.jump_suggestion(1))
        prv = QAction("Previous suggestion\tShift+Tab", self)
        prv.triggered.connect(lambda: self.jump_suggestion(-1))
        a.addAction(nxt)
        a.addAction(prv)
        a.addAction(self._act("Accept selected", self.accept_selected, "A"))
        a.addAction(self._act("Reject selected", self.reject_selected, "X"))
        a.addSeparator()
        a.addAction(self._act("Accept all visible suggestions in loop region",
                              self.accept_in_loop))
        a.addAction(self._act("Clear all pending suggestions", self.clear_pending))

        g = mb.addMenu("&Grid")
        g.addAction(self._act("Set tempo / tap tempo…", self.set_tempo, "Ctrl+T"))
        g.addAction(self._act("Accept detected grid", self.s.accept_grid))
        g.addAction(self._act("Make beat at playhead bar 1", lambda: self.s.set_downbeat_at(self.s.engine.position())))
        g.addAction(self._act("Shift grid 1 frame earlier", lambda: self.s.shift_grid(-1 / self.s.project.frame_rate.fps)))
        g.addAction(self._act("Shift grid 1 frame later", lambda: self.s.shift_grid(1 / self.s.project.frame_rate.fps)))
        g.addAction(self._act("Clear grid", self.s.clear_grid))

        p = mb.addMenu("&Playback")
        p.addAction(self.a_play)
        p.addAction(self.a_start)
        p.addAction(self._act("Go to end", lambda: self._seek_show(self.s.engine.duration), "End"))
        self._act("Go to start", self.go_start, "Return")
        p.addAction(self._act("Preview selection (play from 2 s before)", self.preview, "P"))
        p.addSeparator()
        p.addAction(self._act("Loop in at playhead", lambda: self._loop_point(0), "I"))
        p.addAction(self._act("Loop out at playhead", lambda: self._loop_point(1), "O"))
        p.addAction(self.a_loop)
        p.addAction(self._act("Slower", lambda: self._speed(-1), "["))
        p.addAction(self._act("Faster", lambda: self._speed(1), "]"))
        p.addSeparator()
        p.addAction(self.a_click)
        p.addAction(self.a_blips)

        v = mb.addMenu("&View")
        for act in (self.a_zoom_in, self.a_zoom_out, self.a_fit, self.a_follow):
            v.addAction(act)

        h = mb.addMenu("&Help")
        h.addAction(self._act("Keyboard shortcuts", lambda: ShortcutsDialog(self).exec(), "F1"))
        h.addAction(self._act("About CueForge", self.about))

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.busy_label = QLabel("")
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(220)
        self.prog.setRange(0, 100)
        self.prog.setVisible(False)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.s.cancel_analysis)
        self.audio_label = QLabel("")
        self.audio_label.setStyleSheet(f"color: {theme.FG_DIM};")
        sb.addPermanentWidget(self.busy_label)
        sb.addPermanentWidget(self.prog)
        sb.addPermanentWidget(self.cancel_btn)
        sb.addPermanentWidget(self.audio_label)

    # ================================================================ state sync
    def _project_replaced(self) -> None:
        self.canvas._on_project()
        self._title()
        self._sync_toggles()
        self.a_loop.setChecked(False)
        self._refresh_actions()
        self._rebuild_tap_keys()
        QTimer.singleShot(0, self.timeline._sync_bars)

    def _tracks_changed(self) -> None:
        if self.canvas.pps == 40.0 and self.s.engine.duration and not self.s.project.view:
            self.canvas.zoom_fit()
        self.timeline._sync_bars()

    def _title(self) -> None:
        dirty = "•" if self.s.is_dirty() else ""
        self.setWindowTitle(f"{dirty}{self.s.project.name} — CueForge")

    def _sync_toggles(self) -> None:
        m = self.s.project.mixer
        for a, v in ((self.a_click, m.click_enabled), (self.a_blips, m.blips_enabled)):
            a.blockSignals(True)
            a.setChecked(v)
            a.blockSignals(False)

    def _refresh_actions(self) -> None:
        u = self.s.undo
        self.a_undo.setEnabled(u.can_undo())
        self.a_undo.setText(f"Undo {u.undo_label()}".strip())
        self.a_redo.setEnabled(u.can_redo())
        self.a_redo.setText(f"Redo {u.redo_label()}".strip())

    def _rebuild_tap_keys(self) -> None:
        for sc in self._tap_shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._tap_shortcuts = []
        for lane in self.s.project.lanes:
            k = lane.tap_key.strip().lower()
            if not k or k in RESERVED_KEYS:
                continue
            sc = QShortcut(QKeySequence(k.upper()), self)
            sc.setAutoRepeat(False)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(lambda lid=lane.id: self.s.tap(lid))
            self._tap_shortcuts.append(sc)

    def _clock(self) -> None:
        eng = self.s.engine
        p = self.s.project
        pos = eng.position()
        self.tc_label.setText(seconds_to_tc(pos, p.frame_rate, p.tc_offset))
        g = p.beat_grid
        if g.downbeats and pos >= g.downbeats[0] - 1e-6:
            bar = bisect.bisect_right(g.downbeats, pos + 1e-6) - 1
            beat = bisect.bisect_right(g.beats, pos + 1e-6) - bisect.bisect_left(g.beats, g.downbeats[bar] - 1e-6)
            self.bar_label.setText(f"Bar {bar + 1} · {beat}")
        else:
            self.bar_label.setText("")
        playing = eng.playing
        txt = "❚❚  Pause" if playing else "▶  Play"
        if self.a_play.text() != txt:
            self.a_play.setText(txt)
        backend = {"sounddevice": "Audio: OK", "silent": "Audio: no output device (silent)",
                   "none": "Audio: idle"}.get(eng.backend, eng.backend)
        if self.audio_label.text() != backend:
            self.audio_label.setText(backend)

    def _busy(self, msg: str) -> None:
        self.busy_label.setText(msg)
        analysing = msg.startswith("Analys")
        self.prog.setVisible(analysing)
        self.cancel_btn.setVisible(analysing)
        self.analyse_btn.setEnabled(not analysing)
        if analysing:
            self.prog.setValue(0)

    def _progress(self, frac: float, msg: str) -> None:
        self.prog.setValue(int(frac * 100))
        self.busy_label.setText(msg)

    # ================================================================ transport
    def go_start(self) -> None:
        self.s.engine.seek(self.s.engine.loop[0] if self.a_loop.isChecked() and self.s.engine.loop else 0.0)
        self.canvas.ensure_visible(self.s.engine.position())

    def _seek_show(self, t: float) -> None:
        if not self.s.engine.playing:
            self.s.engine.seek(t)
        self.canvas.ensure_visible(t)

    def _set_snap(self, v: bool) -> None:
        self.s.snap = v
        self.s.settings.set("snap", v)

    def _set_loop(self, v: bool) -> None:
        eng = self.s.engine
        if v and not eng.loop:
            # default: 4 bars from the playhead (or 8 s)
            t = eng.position()
            g = self.s.project.beat_grid
            length = (g.downbeats[1] - g.downbeats[0]) * 4 if len(g.downbeats) > 1 else 8.0
            eng.loop = (t, t + length)
            self.s.project.loop = eng.loop
        eng.loop_enabled = v
        self.canvas.invalidate()

    def _set_follow(self, v: bool) -> None:
        self.canvas.follow = v

    def _loop_point(self, which: int) -> None:
        eng = self.s.engine
        t = eng.position()
        a, b = eng.loop or (0.0, eng.duration or t + 8)
        if which == 0:
            a = t
            if b <= a:
                b = a + 8
        else:
            b = t
            if b <= a:
                a = max(0.0, b - 8)
        eng.loop = (a, b)
        self.s.project.loop = eng.loop
        self.canvas.invalidate()

    def _speed(self, d: int) -> None:
        i = max(0, min(len(SPEEDS) - 1, self.speed.currentIndex() + d))
        self.speed.setCurrentIndex(i)

    def preview(self) -> None:
        t = None
        if self.s.sel_cues:
            t = min(self.s.project.cue(c).time for c in self.s.sel_cues if self.s.project.cue(c))
        elif self.s.sel_sugs:
            t = min(self.s.project.suggestion(x).time for x in self.s.sel_sugs if self.s.project.suggestion(x))
        if t is None:
            return
        self.s.engine.pause()
        self.s.engine.seek(max(0.0, t - 2.0))
        self.canvas.ensure_visible(t)
        self.s.engine.play()

    def _arrow(self, d: int, beat: bool) -> None:
        if self.s.sel_cues:
            if beat:
                self.s.nudge(beats=d)
            else:
                self.s.nudge(frames=d)
            return
        eng = self.s.engine
        t = eng.position()
        g = self.s.project.beat_grid
        if g.beats:
            nt = g.step(t, d)
            if beat and g.downbeats:
                i = bisect.bisect_right(g.downbeats, t + 1e-4) if d > 0 else bisect.bisect_left(g.downbeats, t - 1e-4) - 1
                nt = g.downbeats[i] if 0 <= i < len(g.downbeats) else nt
        else:
            nt = t + d * (4.0 if beat else 1.0)
        if nt is not None:
            eng.seek(max(0.0, nt))
            self.canvas.ensure_visible(nt)

    # ================================================================ editing
    def _undo(self) -> None:
        self.s.undo.undo()

    def _redo(self) -> None:
        self.s.undo.redo()

    def select_all(self) -> None:
        self.s.select(cues={c.id for c in self.s.project.cues})

    def edit_cue(self, cid: str) -> None:
        CueDialog(self.s, cid, self).exec()

    def _edit_selected(self) -> None:
        if len(self.s.sel_cues) == 1:
            self.edit_cue(next(iter(self.s.sel_cues)))

    def jump_suggestion(self, d: int) -> None:
        p = self.s.project
        ref = self.s.engine.position()
        if self.s.sel_sugs:
            times = [p.suggestion(x).time for x in self.s.sel_sugs if p.suggestion(x)]
            if times:
                ref = max(times) if d > 0 else min(times)
        sg = editing.next_suggestion(p, ref, d)
        if sg is None:
            self.statusBar().showMessage("No more suggestions in that direction", 3000)
            return
        self.s.select(sugs={sg.id})
        self.tabs.setCurrentWidget(self.suggestions)
        self._seek_show(sg.time)

    def accept_selected(self) -> None:
        if self.s.sel_sugs:
            last = max(self.s.project.suggestion(x).time for x in self.s.sel_sugs)
            self.s.accept(set(self.s.sel_sugs))
            self._advance_after(last)

    def reject_selected(self) -> None:
        if self.s.sel_sugs:
            last = max(self.s.project.suggestion(x).time for x in self.s.sel_sugs)
            self.s.reject(set(self.s.sel_sugs))
            self._advance_after(last)

    def _advance_after(self, t: float) -> None:
        """After a decision, move straight to the next suggestion for fast review."""
        sg = editing.next_suggestion(self.s.project, t, 1)
        if sg:
            self.s.select(cues=set(self.s.sel_cues), sugs={sg.id})
            if not self.s.engine.playing:
                self._seek_show(sg.time)
            else:
                self.canvas.ensure_visible(sg.time)

    def accept_in_loop(self) -> None:
        loop = self.s.engine.loop
        if not loop:
            QMessageBox.information(self, "Loop region", "Set a loop region first (Shift-drag in the ruler, or I / O).")
            return
        self.s.accept(self.s.visible_ids(t0=loop[0], t1=loop[1]))

    def clear_pending(self) -> None:
        if QMessageBox.question(self, "Clear suggestions", "Remove all pending suggestions?") == QMessageBox.Yes:
            self.s.clear_pending()

    # ================================================================ analysis
    def analyse(self) -> None:
        if self.s.analysis_running():
            return
        if not self.s.audio:
            QMessageBox.information(self, "Analyse", "Import some audio first (File ▸ Import audio).")
            return
        dlg = AnalysisDialog(self.s, self)
        if dlg.exec():
            if not self.s.run_analysis(dlg.options()):
                QMessageBox.information(self, "Analyse", "Audio is still loading — try again in a moment.")

    def _analysis_done(self, res) -> None:
        if isinstance(res, str):
            QMessageBox.warning(self, "Analysis failed", res[:2000])
            return
        self.tabs.setCurrentWidget(self.suggestions)
        n = len(self.s.project.visible_suggestions())
        msg = "\n".join(res.log[-6:])
        self.statusBar().showMessage(f"Analysis done — {n} suggestions visible. Tab to review.", 10000)
        if res.grid and not self.s.project.beat_grid.confirmed:
            msg += "\n\nThe detected beat grid is shown dashed until you accept it (AI Suggestions ▸ Accept grid)."
        QMessageBox.information(self, "Analysis done", msg)

    def set_tempo(self) -> None:
        TempoDialog(self.s, self).exec()

    # ================================================================ files
    def _confirm_discard(self) -> bool:
        if not self.s.is_dirty():
            return True
        r = QMessageBox.question(self, "Unsaved changes", f"Save changes to {self.s.project.name}?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def new_project(self) -> None:
        if self._confirm_discard():
            self.s.new_project()

    def open_project(self, path: str | None = None) -> None:
        if not self._confirm_discard():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open project", self._dir(),
                                                  f"CueForge project (*{EXTENSION})")
        if not path:
            return
        try:
            self.s.open_project(path)
        except Exception as exc:
            QMessageBox.warning(self, "Open failed", str(exc))
            return
        self.s.settings.set("last_dir", os.path.dirname(path))
        missing = [t.name for t in self.s.project.tracks if not os.path.exists(t.path)]
        if missing:
            QMessageBox.warning(self, "Missing audio", "These files could not be found:\n" + "\n".join(missing)
                                + "\n\nUse the ⋯ button on the mixer strip to relink.")

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        for p in self.s.settings.get("recent", []):
            self.recent_menu.addAction(p, lambda path=p: self.open_project(path))
        if not self.recent_menu.actions():
            self.recent_menu.addAction("(none)").setEnabled(False)

    def save(self) -> bool:
        if not self.s.project.path:
            return self.save_as()
        try:
            self.canvas.store_view()
            self.s.save()
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return False
        self._title()
        return True

    def save_as(self) -> bool:
        default = os.path.join(self._dir(), self.s.project.name + EXTENSION)
        path, _ = QFileDialog.getSaveFileName(self, "Save project", default, f"CueForge project (*{EXTENSION})")
        if not path:
            return False
        if not path.endswith(EXTENSION):
            path += EXTENSION
        self.s.project.path = path
        self.s.settings.set("last_dir", os.path.dirname(path))
        return self.save()

    def _autosave(self) -> None:
        p = self.s.project
        if p.path and self.s.is_dirty():
            from ..core.project_io import save_project
            from copy import copy
            try:
                save_project(copy(p), p.path + ".autosave")  # copy: save_project sets path/name
            except OSError:
                pass

    def _dir(self) -> str:
        return self.s.settings.get("last_dir", os.path.expanduser("~"))

    def import_audio(self) -> None:
        exts = " ".join(f"*{e}" for e in AUDIO_EXTENSIONS)
        paths, _ = QFileDialog.getOpenFileNames(self, "Import audio", self._dir(), f"Audio ({exts})")
        if paths:
            self.s.settings.set("last_dir", os.path.dirname(paths[0]))
            self.s.import_audio(paths)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        projects = [p for p in paths if p.endswith(EXTENSION)]
        if projects:
            self.open_project(projects[0])
            return
        audio = [p for p in paths if os.path.splitext(p)[1].lower() in AUDIO_EXTENSIONS]
        if audio:
            self.s.import_audio(audio)

    def project_settings(self) -> None:
        ProjectSettingsDialog(self.s, self).exec()
        self.canvas.invalidate()

    def audio_device(self) -> None:
        AudioDeviceDialog(self.s, self).exec()

    # ================================================================ export
    def _export_path(self, title: str, suffix: str, filt: str) -> str:
        default = os.path.join(self._dir(), (self.s.project.export.ma3_name or self.s.project.name) + suffix)
        path, _ = QFileDialog.getSaveFileName(self, title, default, filt)
        if path and not path.lower().endswith(suffix.lower()):
            path += suffix
        return path

    def _check_pending(self) -> bool:
        n = len([x for x in self.s.project.suggestions if x.status == "pending"])
        if n and self.s.settings.get("warn_pending", True):
            r = QMessageBox.question(self, "Pending suggestions",
                                     f"{n} AI suggestion(s) are still pending and will NOT be exported "
                                     "(only confirmed cues are). Continue?")
            return r == QMessageBox.Yes
        return True

    def export_ma3(self) -> None:
        from ..export.ma3 import build_ma3_macro_commands, export_lanes, export_ma3_lua, export_ma3_xml
        if not export_lanes(self.s.project):
            QMessageBox.information(self, "Export", "No cues to export (check the lanes' Export setting).")
            return
        if not self._check_pending():
            return
        dlg = MA3ExportDialog(self.s, self)
        if not dlg.exec():
            return
        fmt = self.s.settings.get("ma3_format", "xml")
        tc = int(self.s.settings.get("ma3_tc_number", 1))
        try:
            if fmt == "xml":
                path = self._export_path("Export grandMA3 timecode XML", ".xml", "XML (*.xml)")
                if path:
                    n = export_ma3_xml(self.s.project, path, tc)
                    self._exported(path, f"{n} cue events written.\n\nOn the console: copy the file to "
                                         "gma3_library/datapools/timecodes (on a USB stick or onPC library "
                                         "folder), then Import it into the Timecode pool.")
            elif fmt == "lua":
                path = self._export_path("Export grandMA3 Lua plugin", ".lua", "Lua (*.lua)")
                if path:
                    export_ma3_lua(self.s.project, path, tc, dlg.create.isChecked())
                    self._exported(path, "Import the plugin into a Plugin pool slot and run it. "
                                         f"It writes Timecode {tc}.")
            else:
                path = self._export_path("Export command list", ".txt", "Text (*.txt)")
                if path:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("\n".join(build_ma3_macro_commands(self.s.project)) + "\n")
                    self._exported(path, "Paste these into a macro or the command line to create the cues.")
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def _exported(self, path: str, msg: str) -> None:
        self.s.settings.set("last_dir", os.path.dirname(path))
        QMessageBox.information(self, "Exported", f"Saved {os.path.basename(path)}\n\n{msg}")

    def export_csv(self) -> None:
        from ..export.csv_export import export_csv
        path = self._export_path("Export CSV cue list", ".csv", "CSV (*.csv)")
        if path:
            try:
                n = export_csv(self.s.project, path)
                self._exported(path, f"{n} cues.")
            except OSError as exc:
                QMessageBox.warning(self, "Export failed", str(exc))

    def export_ltc(self) -> None:
        import soundfile as sf
        from ..audio.loader import resample
        from ..export.ltc import render_ltc_for_project
        dlg = LTCDialog(self.s, self)
        if not dlg.exec():
            return
        path = self._export_path("Render LTC", "_LTC.wav", "WAV (*.wav)")
        if not path:
            return
        p = self.s.project
        ex = p.export
        end = max(self.s.engine.duration, p.cues[-1].time if p.cues else 0) + 1.0
        sig, t0 = render_ltc_for_project(-ex.ltc_preroll, end, p.tc_offset, p.frame_rate,
                                         ex.ltc_sample_rate, ex.ltc_level_db)
        if dlg.stereo.isChecked():
            mix = self.s.engine.bounce(max(0.0, t0), t0 + len(sig) / ex.ltc_sample_rate)
            mono = resample(mix.mean(axis=1, keepdims=True), self.s.engine.sr, ex.ltc_sample_rate)[:, 0]
            pad = int(round(max(0.0, -t0) * ex.ltc_sample_rate))
            left = np.zeros(len(sig), np.float32)
            n = min(len(mono), len(sig) - pad)
            if n > 0:
                left[pad:pad + n] = mono[:n]
            data = np.stack([left, sig], axis=1)
        else:
            data = sig
        try:
            sf.write(path, data, ex.ltc_sample_rate, subtype="PCM_24")
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        msg = (f"LTC starts at {seconds_to_tc(t0, p.frame_rate, p.tc_offset)} "
               f"({max(0.0, -t0):.2f} s before the song).")
        if -t0 < ex.ltc_preroll - 0.05:
            msg += ("\n\nPre-roll was shortened because the song starts too close to 00:00:00:00. "
                    "Set a start timecode such as 01:00:00:00 in Project settings.")
        self._exported(path, msg)

    # ================================================================ misc
    def about(self) -> None:
        QMessageBox.about(self, "About CueForge",
                          f"<b>CueForge {__version__}</b><br>Plan lighting hits and cues against audio, "
                          "with AI suggestions you confirm.<br><br>Export: grandMA3 timecode, CSV, LTC.")

    def closeEvent(self, e) -> None:
        if not self._confirm_discard():
            e.ignore()
            return
        self.s.engine.close()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().removeEventFilter(self)
        from .workers import wait_all
        wait_all(2000)
        self.s.settings.set("geometry", self.saveGeometry().toHex().data().decode())
        self.s.settings.set("state", self.saveState().toHex().data().decode())
        e.accept()

    def restore_layout(self) -> None:
        from PySide6.QtCore import QByteArray
        g, st = self.s.settings.get("geometry"), self.s.settings.get("state")
        if g:
            self.restoreGeometry(QByteArray.fromHex(g.encode()))
        if st:
            self.restoreState(QByteArray.fromHex(st.encode()))
