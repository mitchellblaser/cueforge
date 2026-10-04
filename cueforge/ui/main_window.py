"""Main window."""
from __future__ import annotations

import bisect
import os
import time

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QDockWidget, QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QTabWidget, QToolBar, QWidget, QSizePolicy)

from .. import __version__
from ..audio.loader import AUDIO_EXTENSIONS
from ..core import editing
from ..core.project_io import EXTENSION
from ..core.timecode import seconds_to_tc
from . import theme
from .dialogs import (AnalysisDialog, AudioDeviceDialog, CueDialog, LTCDialog, MA3ExportDialog,
                      PatternDialog, ProjectSettingsDialog, SectionDialog, ShortcutsDialog, SongDialog,
                      TempoDialog)
from .mixer import MixerPanel
from .panels import CueTable, LanePanel, SongList, SuggestionPanel
from .session import RESERVED_KEYS, Session
from .timeline import TimelinePanel

SPEEDS = [0.5, 0.75, 1.0]


class PanelsWindow(QMainWindow):
    """Second window holding the panels in dual-monitor mode."""

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._closing = False
        self.setWindowTitle("CueForge — Panels")
        self.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowTabbedDocks)
        self.setCentralWidget(None)

    def close_for_real(self) -> None:
        self._closing = True
        self.close()
        self.deleteLater()

    def closeEvent(self, e) -> None:
        if not self._closing:            # closing the panels window returns the panels
            e.ignore()
            self.main.dual_monitor(False)
            return
        e.accept()


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
        self.canvas.rename_section_requested.connect(lambda sid: SectionDialog(self.s, sid, self).exec())
        self.canvas.pattern_fill_requested.connect(lambda a, b: PatternDialog(self.s, a, b, self).exec())

        # right dock: suggestions / cues / lanes
        self.suggestions = SuggestionPanel(self.s)
        self.suggestions.analyse_requested.connect(self.analyse)
        self.suggestions.tempo_requested.connect(self.set_tempo)
        self.suggestions.seek_requested.connect(self._seek_show)
        self.cue_table = CueTable(self.s)
        self.cue_table.seek_requested.connect(self._seek_show)
        self.lanes = LanePanel(self.s)
        # every panel is its own dock: pop any of them out, or move them to a second
        # screen (View ▸ Dual-monitor layout)
        self.dock_sugs = self._dock("AI Suggestions", "dock_suggestions", self.suggestions, Qt.RightDockWidgetArea)
        self.dock_sugs.setMinimumWidth(380)
        self.dock_cues = self._dock("Cue list", "dock_cuelist", self.cue_table, Qt.RightDockWidgetArea)
        self.dock_lanes = self._dock("Lanes", "dock_lanes", self.lanes, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(self.dock_sugs, self.dock_cues)
        self.tabifyDockWidget(self.dock_cues, self.dock_lanes)
        self.dock_sugs.raise_()
        for d in (self.dock_sugs, self.dock_cues, self.dock_lanes):
            # an Inspector panel that is docked again rejoins the others as a tab
            d.topLevelChanged.connect(lambda floating: floating or QTimer.singleShot(0, self.retab_inspector))
            d.dockLocationChanged.connect(lambda _a: QTimer.singleShot(0, self.retab_inspector))

        self.setlist = SongList(self.s)
        self.setlist.open_settings.connect(self.song_settings)
        self.setlist.add_requested.connect(self.add_song)
        self.dock_setlist = self._dock("Setlist", "setlist", self.setlist, Qt.LeftDockWidgetArea)
        self.dock_setlist.setMinimumWidth(250)

        self.mixer = MixerPanel(self.s)
        self.dock_mixer = self._dock("Mixer", "mixer", self.mixer, Qt.BottomDockWidgetArea)
        self.panels_window = None

        self._build_toolbar()
        from ..control.hub import ControlHub
        self.control = ControlHub(self.s, {
            "transport:toggle": self.s.engine.toggle,
            "transport:stop": lambda: (self.s.engine.pause(), self.go_start()),
            "sugg:next": lambda: self.jump_suggestion(1),
            "sugg:prev": lambda: self.jump_suggestion(-1),
            "sugg:accept": self.accept_selected,
            "sugg:reject": self.reject_selected,
            "edit:undo": self._undo,
            "section:add": lambda: self.s.add_section_at(),
            "song:next": lambda: self._step_song(1),
            "song:prev": lambda: self._step_song(-1),
            "loop:toggle": lambda: self.a_loop.toggle(),
        })
        self.control.status.connect(lambda m: self.statusBar().showMessage(m, 5000))
        from ..control.ma3link import MA3Link
        self.ma3link = MA3Link(self.s)
        self.ma3link.status.connect(lambda m: self.statusBar().showMessage(m, 5000))
        self._build_menus()
        self._build_statusbar()
        self._tap_shortcuts: list[QShortcut] = []
        self._rebuild_tap_keys()

        s = self.s
        s.lanes_changed.connect(self._rebuild_tap_keys)
        s.lanes_changed.connect(self._sync_lane_combo)
        s.active_lane_changed.connect(self._sync_lane_combo)
        s.project_replaced.connect(self._sync_lane_combo)
        s.selection_changed.connect(self._sel_hold)
        s.song_will_change.connect(self.canvas.store_view)
        s.songs_changed.connect(self._title)
        s.project_replaced.connect(self._project_replaced)
        s.dirty_changed.connect(lambda _: self._title())
        s.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        s.busy.connect(self._busy)
        s.progress.connect(self._progress)
        s.grid_rephased.connect(self._grid_rephased)
        s.auto_advance_changed.connect(self._sync_advance)
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
        from PySide6.QtWidgets import QAbstractSpinBox, QLineEdit, QPlainTextEdit, QTextEdit
        et = ev.type()
        if getattr(self, "_held", None):
            if et == QEvent.KeyRelease and not ev.isAutoRepeat():
                self._tap_release(ev.text(), ev.key())          # matched on the key, not its text
            elif et in (QEvent.ApplicationStateChange, QEvent.WindowDeactivate) and \
                    QApplication.applicationState() != Qt.ApplicationActive:
                self._finish_hold()                             # the release would go elsewhere
        if et in (QEvent.ShortcutOverride, QEvent.KeyPress) and isinstance(obj, QWidget) \
                and isinstance(obj, (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)):
            mods = ev.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
            if et == QEvent.ShortcutOverride and not mods:
                # typing in a text box: Return, Space, letters, digits… belong to the box, not
                # to the transport / tap shortcuts (Return = go to start, Q, W, 1-9 …)
                ev.accept()
                return False
            if et == QEvent.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape) \
                    and obj.window() in (self, self.panels_window) and not isinstance(obj, QPlainTextEdit):
                # after committing a label / number, hand the keyboard back to the timeline
                if obj.window() is self:
                    QTimer.singleShot(0, self.canvas.setFocus)
                else:
                    QTimer.singleShot(0, obj.clearFocus)        # panels window: shortcuts are app-wide
        if ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Tab, Qt.Key_Backtab) \
                and isinstance(obj, QWidget) and obj.window() in (self, self.panels_window):
            if not isinstance(obj, (QLineEdit, QPlainTextEdit, QAbstractSpinBox)):
                back = ev.key() == Qt.Key_Backtab or bool(ev.modifiers() & Qt.ShiftModifier)
                self.jump_suggestion(-1 if back else 1)
                return True
        return False

    # ================================================================ building
    def _dock(self, title: str, name: str, widget, area) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable |
                      QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, d)
        return d

    def _show_panel(self, dock: QDockWidget) -> None:
        dock.show()
        dock.raise_()

    def _docks(self) -> list[QDockWidget]:
        return [self.dock_setlist, self.dock_sugs, self.dock_cues, self.dock_lanes, self.dock_mixer]

    def retab_inspector(self) -> None:
        """Keep the docked Inspector panels (AI Suggestions, Cue list, Lanes) as tabs of one
        group in whichever window holds them; floating panels are left alone."""
        if getattr(self, "_retabbing", False):
            return
        self._retabbing = True
        try:
            docks = [d for d in (self.dock_sugs, self.dock_cues, self.dock_lanes)
                     if not d.isFloating() and not d.isHidden()]
            by_win: dict[int, list[QDockWidget]] = {}
            for d in docks:
                win = d.parentWidget()
                if isinstance(win, QMainWindow):
                    by_win.setdefault(id(win), []).append(d)
            for group in by_win.values():
                if len(group) < 2:
                    continue
                win = group[0].parentWidget()
                anchor = max(group, key=lambda d: len(win.tabifiedDockWidgets(d)))
                current = next((d for d in group if not d.visibleRegion().isEmpty()), anchor)
                joined = set(win.tabifiedDockWidgets(anchor)) | {anchor}
                moved = False
                for d in group:
                    if d not in joined:
                        win.tabifyDockWidget(anchor, d)
                        joined.add(d)
                        moved = True
                if moved:
                    current.raise_()
        finally:
            self._retabbing = False

    def pop_out(self, dock: QDockWidget) -> None:
        dock.show()
        dock.setFloating(True)
        dock.resize(max(380, dock.width()), max(420, dock.height()))
        dock.raise_()

    # dual monitor ------------------------------------------------------
    def dual_monitor(self, on: bool | None = None) -> None:
        """Move Setlist / Inspector / Mixer into a second window, on the second screen if
        there is one; the main window keeps the full-width timeline."""
        from PySide6.QtGui import QGuiApplication
        on = self.panels_window is None if on is None else on
        if on and self.panels_window is None:
            pw = PanelsWindow(self)
            for d in self._docks():
                self.removeDockWidget(d)
                d.setFloating(False)
            pw.addDockWidget(Qt.LeftDockWidgetArea, self.dock_setlist)
            for d in (self.dock_sugs, self.dock_cues, self.dock_lanes):
                pw.addDockWidget(Qt.RightDockWidgetArea, d)
            pw.tabifyDockWidget(self.dock_sugs, self.dock_cues)
            pw.tabifyDockWidget(self.dock_cues, self.dock_lanes)
            pw.addDockWidget(Qt.BottomDockWidgetArea, self.dock_mixer)
            for d in self._docks():
                d.show()
            self.dock_sugs.raise_()
            screens = QGuiApplication.screens()
            mine = self.screen()
            other = next((sc for sc in screens if sc is not mine), None)
            if other is not None:
                pw.setGeometry(other.availableGeometry())
                pw.showMaximized()
                self.statusBar().showMessage("Dual-monitor layout: panels on the second screen", 5000)
            else:
                g = mine.availableGeometry() if mine else None
                pw.resize(1000, 800)
                pw.show()
                self.statusBar().showMessage("Only one screen found — panels opened in their own window", 6000)
            self.panels_window = pw
            self._set_shortcut_scope(Qt.ApplicationShortcut)
            self.s.settings.set("dual_monitor", True)
        elif not on and self.panels_window is not None:
            pw = self.panels_window
            for d in self._docks():
                pw.removeDockWidget(d)
            self.addDockWidget(Qt.LeftDockWidgetArea, self.dock_setlist)
            for d in (self.dock_sugs, self.dock_cues, self.dock_lanes):
                self.addDockWidget(Qt.RightDockWidgetArea, d)
            self.tabifyDockWidget(self.dock_sugs, self.dock_cues)
            self.tabifyDockWidget(self.dock_cues, self.dock_lanes)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.dock_mixer)
            for d in self._docks():
                d.show()
            self.panels_window = None
            pw.close_for_real()
            self._set_shortcut_scope(Qt.WindowShortcut)
            self.s.settings.set("dual_monitor", False)
        if hasattr(self, "a_dual"):
            self.a_dual.blockSignals(True)
            self.a_dual.setChecked(self.panels_window is not None)
            self.a_dual.blockSignals(False)

    def _set_shortcut_scope(self, ctx) -> None:
        """With panels in a second window, shortcuts (Space, Q, W, tap keys…) must work
        from either window."""
        for a in self.actions():
            a.setShortcutContext(ctx)
        for sc in getattr(self, "_tap_shortcuts", []):
            sc.setContext(ctx)

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
        tb.addAction(self.a_snap)
        self.snap_div = QComboBox()
        for txt, d in (("1 beat", 1), ("½ beat", 2), ("¼ beat", 4)):
            self.snap_div.addItem(txt, d)
        self.snap_div.setToolTip("Snap resolution: cues, drags and held Temps snap to beats, half or quarter beats")
        self.snap_div.setFocusPolicy(Qt.NoFocus)
        self.snap_div.activated.connect(lambda _i: self._set_snap_div(self.snap_div.currentData()))
        tb.addWidget(self.snap_div)
        for a in (self.a_loop, self.a_click, self.a_blips, self.a_follow):
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
        self._build_cue_bar()
        # extra zoom keys
        self._act("Zoom in", lambda: self.canvas.zoom(1.5), "=")

    def _build_cue_bar(self) -> None:
        """Second toolbar row: fast cue entry into the active lane."""
        self.addToolBarBreak()
        tb = QToolBar("Cues")
        tb.setObjectName("cuebar")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel("  Active lane "))
        self.lane_combo = QComboBox()
        self.lane_combo.setMinimumWidth(150)
        self.lane_combo.setToolTip("Lane that ＋ Cue / ＋ Temp drop into. Also: click a lane header, or ↑ / ↓")
        self.lane_combo.activated.connect(lambda _: self.s.set_active_lane(self.lane_combo.currentData()))
        tb.addWidget(self.lane_combo)
        tb.addSeparator()
        self.a_add_cue = self._act("＋ Cue", lambda: self._drop(False), "Q",
                                   tip="Add a cue at the playhead in the active lane (Q) — works while playing")
        self.a_add_temp = self._act("＋ Temp", lambda: self._drop(True), "W",
                                    tip="Add a Temp (cue with a hold time: Temp On, then Temp Off after the hold) "
                                        "at the playhead in the active lane (W)")
        tb.addAction(self.a_add_cue)
        tb.addAction(self.a_add_temp)
        tb.addWidget(QLabel("  Hold "))
        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.05, 60.0)
        self.hold_spin.setDecimals(2)
        self.hold_spin.setSingleStep(0.1)
        self.hold_spin.setSuffix(" s")
        self.hold_spin.setValue(self.s.temp_hold)
        self.hold_spin.setToolTip("Hold time for new Temps. With a cue selected, also changes that cue's hold.")
        self.hold_spin.valueChanged.connect(self._hold_changed)
        tb.addWidget(self.hold_spin)
        beat_btn = QPushButton("= 1 beat")
        beat_btn.setToolTip("Set the hold to one beat of the grid")
        beat_btn.clicked.connect(self._hold_one_beat)
        tb.addWidget(beat_btn)
        self.cue_hint = QLabel("")
        self.cue_hint.setStyleSheet(f"color: {theme.FG_DIM}; padding-left: 12px;")
        tb.addWidget(self.cue_hint)
        self._act("Previous lane", lambda: self.s.step_active_lane(-1), "Up")
        self._act("Next lane", lambda: self.s.step_active_lane(1), "Down")
        self._sync_lane_combo()

    # ================================================================ arrange
    def _section_here(self):
        from ..core import arrange
        sid = self.s.sel_section if any(m.id == self.s.sel_section for m in self.s.project.sections) else ""
        m = next((x for x in self.s.project.sections if x.id == sid), None)
        return m or arrange.section_at(self.s.project, self.s.engine.position())

    def _rename_section_here(self) -> None:
        m = self._section_here()
        if m:
            SectionDialog(self.s, m.id, self).exec()

    def _copy_section_here(self) -> None:
        m = self._section_here()
        if m:
            self.s.copy_section_to_repeats(m.id)
        else:
            self.statusBar().showMessage("No section here — add markers with M", 4000)

    def pattern_fill(self) -> None:
        """Range: loop region, else selected section, else section at playhead, else selected
        cues, else one bar from the playhead."""
        s = self.s
        rng = None
        if s.engine.loop and (s.engine.loop_enabled or self.a_loop.isChecked()):
            rng = s.engine.loop
        if rng is None:
            m = self._section_here()
            if m:
                rng = s.section_range(m.id)
        if rng is None and len(s.sel_cues) > 1:
            ts = [s.project.cue(c).time for c in s.sel_cues if s.project.cue(c)]
            rng = (min(ts), max(ts) + 1e-3)
        if rng is None:
            t = s.engine.position()
            g = s.project.beat_grid
            bar = (g.downbeats[1] - g.downbeats[0]) if len(g.downbeats) > 1 else 2.0
            rng = (t, t + bar)
        PatternDialog(s, rng[0], rng[1], self).exec()

    def _sel_hold(self) -> None:
        """Selecting a Temp shows its hold in the Hold box (and edits it there)."""
        sel = [self.s.project.cue(c) for c in self.s.sel_cues]
        if len(sel) == 1 and sel[0] and sel[0].duration:
            self.hold_spin.blockSignals(True)
            self.hold_spin.setValue(sel[0].duration)
            self.hold_spin.blockSignals(False)
        if len(sel) == 1 and sel[0]:
            self.s.set_active_lane(sel[0].lane_id)

    def _sync_lane_combo(self) -> None:
        self.lane_combo.blockSignals(True)
        self.lane_combo.clear()
        for lane in self.s.project.lanes:
            key = f"  [{lane.tap_key.upper()}]" if lane.tap_key else ""
            self.lane_combo.addItem(f"{lane.name}{key}", lane.id)
        self.lane_combo.setCurrentIndex(max(0, self.lane_combo.findData(self.s.active_lane_id)))
        self.lane_combo.blockSignals(False)
        lane = self.s.project.lane(self.s.active_lane_id)
        if lane:
            self.lane_combo.setStyleSheet(f"QComboBox {{ border: 1px solid {lane.color}; color: {lane.color}; }}")
            self.cue_hint.setText(f"Q / W drop into {lane.name} (MA3 Seq {lane.ma3_sequence + self.s.project.song.seq_offset})")

    def _drop(self, temp: bool) -> None:
        c = self.s.add_at_playhead(temp)
        if c is not None:
            self.canvas.ensure_visible(c.time)

    def _hold_changed(self, v: float) -> None:
        self.s.set_temp_hold(v)
        sel = [self.s.project.cue(c) for c in self.s.sel_cues]
        sel = [c for c in sel if c and c.duration]
        if len(sel) == 1 and abs(sel[0].duration - v) > 1e-6:
            self.s.update_cue(sel[0].id, duration=v)

    def _hold_one_beat(self) -> None:
        g = self.s.project.beat_grid
        if len(g.beats) > 1:
            self.hold_spin.setValue(round(g.bpm() and 60.0 / g.bpm(), 3))

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
        f.addAction(self._act("Import audio into this song…", self.import_audio, "Ctrl+I"))
        f.addSeparator()
        f.addAction(self._act("Add song to setlist…", self.add_song, "Ctrl+Shift+N"))
        f.addAction(self._act("Add songs from files (one song per file)…", self.add_songs_files))
        f.addAction(self._act("Add songs from a folder (one song per sub-folder)…", self.add_songs_folder))
        f.addAction(self._act("Song settings…", lambda: self.song_settings(self.s.project.song.id)))
        f.addAction(self._act("Previous song", lambda: self._step_song(-1), "Ctrl+PgUp"))
        f.addAction(self._act("Next song", lambda: self._step_song(1), "Ctrl+PgDown"))
        f.addSeparator()
        f.addAction(self._act("Import grandMA3 timecode XML…", self.import_ma3))
        f.addAction(self._act("Import CuePoints CSV / spreadsheet…", self.import_cuepoints))
        ex = f.addMenu("Export")
        ex.addAction(self._act("grandMA3…", self.export_ma3, "Ctrl+E"))
        ex.addAction(self._act("CSV cue list…", self.export_csv))
        ex.addAction(self._act("LTC timecode audio…", self.export_ltc))
        f.addSeparator()
        f.addAction(self._act("Project settings (frame rate, TC offset)…", self.project_settings))
        f.addAction(self._act("Audio output…", self.audio_device))
        f.addAction(self._act("MIDI && OSC control…", self.control_settings))
        f.addAction(self._act("grandMA3 live link…", self.link_settings, "Ctrl+L"))
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
        e.addAction(self._act("Make selected Temp", lambda: self.s.set_selected_temp(True), "Shift+W"))
        e.addAction(self._act("Make selected normal cue", lambda: self.s.set_selected_temp(False), "Shift+Q"))
        e.addAction(self._act("Edit selected cue…", self._edit_selected, "Ctrl+Return"))
        e.addSeparator()
        self._act("Nudge left", lambda: self._arrow(-1, False), "Left")
        self._act("Nudge right", lambda: self._arrow(1, False), "Right")
        self._act("Nudge left beat", lambda: self._arrow(-1, True), "Shift+Left")
        self._act("Nudge right beat", lambda: self._arrow(1, True), "Shift+Right")
        e.addAction(self._act("Add lane", lambda: self.s.add_lane()))

        ar = mb.addMenu("A&rrange")
        ar.addAction(self._act("Copy cues", self.s.copy_selected, QKeySequence.Copy))
        ar.addAction(self._act("Cut cues", self.s.cut_selected, QKeySequence.Cut))
        ar.addAction(self._act("Paste at playhead", lambda: self.s.paste_at(), QKeySequence.Paste))
        ar.addAction(self._act("Paste into section at playhead (aligned to its start)",
                               lambda: self.s.paste_into_section(), "Ctrl+Shift+V"))
        ar.addSeparator()
        ar.addAction(self._act("Add section marker at playhead", lambda: self.s.add_section_at(), "M"))
        ar.addAction(self._act("Rename section at playhead…", self._rename_section_here, "Shift+M"))
        ar.addAction(self._act("Copy this section's cues to all its repeats", self._copy_section_here,
                               "Ctrl+Shift+C"))
        ar.addAction(self._act("Create sections from AI suggestions", self.s.sections_from_ai))
        ar.addSeparator()
        ar.addAction(self._act("Pattern fill…", self.pattern_fill, "Ctrl+P"))

        a = mb.addMenu("&AI")
        a.addAction(self._act("Analyse audio…", self.analyse, "Ctrl+R"))
        a.addAction(self._act("Analyse all songs in the setlist…", lambda: self.analyse(all_songs=True),
                              "Ctrl+Shift+R"))
        a.addAction(self._act("AI models (install / remove)…", self.ai_models))
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
        self.a_advance = self._act("Jump to the next suggestion after Accept / Reject", self.set_auto_advance,
                                   checkable=True)
        self.a_advance.setChecked(self.s.auto_advance)
        a.addAction(self.a_advance)
        a.addSeparator()
        a.addAction(self._act("Accept all visible suggestions in loop region",
                              self.accept_in_loop))
        a.addAction(self._act("Clear all pending suggestions", self.clear_pending))

        g = mb.addMenu("&Grid")
        g.addAction(self._act("Set tempo / tap tempo…", self.set_tempo, "Ctrl+T"))
        g.addAction(self._act("Accept detected grid", self.s.accept_grid))
        g.addSeparator()
        g.addAction(self._act("Set bar 1 here (beat at playhead; tap D on the 'one' while playing)",
                              lambda: self.s.set_downbeat_at(self.s.engine.position()), "D"))
        g.addAction(self._act("Move bar 1 one beat earlier", lambda: self.s.move_bar_one(-1), "Ctrl+Alt+Left"))
        g.addAction(self._act("Move bar 1 one beat later", lambda: self.s.move_bar_one(1), "Ctrl+Alt+Right"))
        g.addAction(self._act("Remove beats before bar 1 (gap / count-in)", self.s.drop_beats_before_bar_one))
        g.addSeparator()
        g.addAction(self._act("Shift grid 1 frame earlier", lambda: self.s.shift_grid(-1 / self.s.project.frame_rate.fps)))
        g.addAction(self._act("Shift grid 1 frame later", lambda: self.s.shift_grid(1 / self.s.project.frame_rate.fps)))
        g.addSeparator()
        g.addAction(self._act("Halve tempo (grid is on 8th notes)", self.s.halve_tempo))
        g.addAction(self._act("Double tempo (grid is on half notes)", self.s.double_tempo))
        self.a_tapgrid = self._act("Tap-along grid (live): press T on every beat", self._tap_grid_mode,
                                   checkable=True, tip="Build or fix the grid by tapping along; taps snap to the drums")
        g.addAction(self.a_tapgrid)
        self._act("Tap grid beat", self.s.tap_grid_beat, "T")
        g.addSeparator()
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
        self.a_scrub = self._act("Scrub audio (hear audio while dragging the playhead)", self._set_scrub,
                                 "Ctrl+Alt+S", True)
        self.a_scrub.setChecked(self.canvas.scrub_audio)
        v.addAction(self.a_scrub)
        v.addSeparator()
        panels = v.addMenu("Panels")
        for d in self._docks():
            panels.addAction(d.toggleViewAction())
        pop = v.addMenu("Pop out")
        for d in self._docks():
            pop.addAction(d.windowTitle(), lambda d=d: self.pop_out(d))
        self.a_dual = self._act("Dual-monitor layout (panels on the second screen)",
                                lambda on: self.dual_monitor(on), "Ctrl+Shift+D", True)
        v.addAction(self.a_dual)
        v.addAction(self._act("Reset panel layout", self.reset_layout))
        self.view_menu = v

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
        self.reanalyse_btn = QPushButton("↻ Re-analyse with the new bars")
        self.reanalyse_btn.setToolTip("Bar 1 moved: sections, fills and chord changes were found with the old "
                                      "bar lines. Re-run the analysis on your grid (your cues are not touched).")
        self.reanalyse_btn.setVisible(False)
        self.reanalyse_btn.clicked.connect(self.reanalyse)
        sb.addPermanentWidget(self.reanalyse_btn)
        self.link_label = QLabel("")
        self.link_label.setStyleSheet("color: #66bb6a; padding-left: 8px;")
        sb.addPermanentWidget(self.link_label)

    # ================================================================ state sync
    def _project_replaced(self) -> None:
        self.canvas._on_project()
        self._sync_snap_div()
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
        p = self.s.project
        song = f" — {p.song.name}" if len(p.songs) > 1 or p.song.name != "Song 1" else ""
        self.setWindowTitle(f"{dirty}{p.name}{song} — CueForge")

    # ================================================================ setlist
    def add_song(self) -> None:
        exts = " ".join(f"*{e}" for e in AUDIO_EXTENSIONS)
        paths, _ = QFileDialog.getOpenFileNames(self, "Audio for the new song (mix, stems, click…)", self._dir(),
                                                f"Audio ({exts})")
        if paths:
            self.s.settings.set("last_dir", os.path.dirname(paths[0]))
            self.s.add_song(paths)

    def add_songs_files(self) -> None:
        from ..core.bulk import song_groups_from_files
        exts = " ".join(f"*{e}" for e in AUDIO_EXTENSIONS)
        paths, _ = QFileDialog.getOpenFileNames(self, "Audio files — each becomes a song", self._dir(),
                                                f"Audio ({exts})")
        if paths:
            self.s.settings.set("last_dir", os.path.dirname(paths[0]))
            self._bulk_added(self.s.add_songs(song_groups_from_files(paths)))

    def add_songs_folder(self) -> None:
        from ..core.bulk import song_groups_from_folder
        d = QFileDialog.getExistingDirectory(self, "Folder of songs (each sub-folder = a song with its stems)",
                                             self._dir())
        if not d:
            return
        self.s.settings.set("last_dir", d)
        groups = song_groups_from_folder(d)
        if not groups:
            QMessageBox.information(self, "Add songs", "No audio files found in that folder.")
            return
        self._bulk_added(self.s.add_songs(groups))

    def _bulk_added(self, ids: list[str]) -> None:
        if not ids:
            return
        n = len(ids)
        if QMessageBox.question(self, "Songs added",
                                f"Added {n} song(s) to the setlist.\n\nAnalyse them all now? It runs in the "
                                "background — you can keep working, or leave it running.") == QMessageBox.Yes:
            self.analyse(all_songs=True, song_ids=ids)

    def song_settings(self, sid: str) -> None:
        if sid and self.s.project.song_by_id(sid):
            SongDialog(self.s, sid, self).exec()
            self._title()
            self.canvas.invalidate()

    def _step_song(self, d: int) -> None:
        p = self.s.project
        i = p.current + d
        if 0 <= i < len(p.songs):
            self.s.switch_song(p.songs[i].id)

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
            sc.setContext(Qt.ApplicationShortcut if self.panels_window is not None else Qt.WindowShortcut)
            sc.activated.connect(lambda lid=lane.id, key=k: self._tap_press(lid, key))
            self._tap_shortcuts.append(sc)

    # press & hold a lane key: tap = cue, hold = Temp for as long as the key is down
    HOLD_MIN = 0.22          # seconds held before a tap becomes a Temp

    def _tap_press(self, lane_id: str, key: str) -> None:
        if getattr(self, "_held", None):
            self._finish_hold()                     # another key was still held: finish that one
        c = self.s.tap(lane_id)
        if c is None:
            return
        eng = self.s.engine
        try:
            code = QKeySequence(key.upper())[0].key()
        except Exception:
            code = None
        pos = eng.position()
        self._held = {"key": key.lower(), "code": code, "cue": c.id, "lane": lane_id, "t0": pos, "last": pos,
                      "playing": eng.playing}
        if not hasattr(self, "_hold_timer"):
            self._hold_timer = QTimer(self)
            self._hold_timer.setInterval(30)
            self._hold_timer.timeout.connect(self._hold_tick)
        self._hold_timer.start()

    def _hold_tick(self) -> None:
        """While the key is down the Temp grows on the timeline (an overlay: no re-render).
        Playback stopping, a jump or loop wrap ends the hold."""
        h = getattr(self, "_held", None)
        if not h:
            self._hold_timer.stop()
            return
        c = self.s.project.cue(h["cue"])
        eng = self.s.engine
        pos = eng.position()
        if c is None or not h["playing"] or not eng.playing or pos < h["last"] - 0.05:
            self._finish_hold()
            return
        h["last"] = pos
        if pos - h["t0"] >= self.HOLD_MIN:
            self.canvas.hold_preview = (h["lane"], c.time, pos)
            self.canvas.update()

    def _tap_release(self, key: str | None = None, code=None) -> None:
        h = getattr(self, "_held", None)
        if not h:
            return
        if code is not None and h.get("code") is not None:
            if code != h["code"]:
                return
        elif key is not None and h["key"] != key.lower():
            return
        self._finish_hold()

    def _finish_hold(self) -> None:
        """End the hold: a short press stays a cue; a long one becomes a Temp whose end
        snaps to the grid. Always leaves the cue in a finished, saved state."""
        h, self._held = getattr(self, "_held", None), None
        if hasattr(self, "_hold_timer"):
            self._hold_timer.stop()
        self.canvas.hold_preview = None
        self.canvas.update()
        if not h:
            return
        p = self.s.project
        c = p.cue(h["cue"])
        if c is None:
            return
        eng = self.s.engine
        pos = eng.position()
        end = pos if (h["playing"] and eng.playing and pos >= h["last"] - 0.05) else h["last"]
        if not h["playing"] or end - h["t0"] < self.HOLD_MIN:
            return                                    # a tap: the normal cue stays
        if self.s.snap:
            g = editing.grid_point(p, end)
            if g is not None:
                end = g
        step = 1.0 / p.frame_rate.fps
        if self.s.snap and len(p.beat_grid.beats) > 1:
            step = max(step, float(np.median(np.diff(p.beat_grid.beats))) / max(1, p.snap_div))
        c.duration = round(max(step, end - c.time), 3)   # at least one grid step
        p.sort_cues()
        self.s._sync_engine()
        self.s.cues_changed.emit()
        self.s._touch()
        lane = p.lane(c.lane_id)
        self.statusBar().showMessage(f"Temp in {lane.name if lane else '?'}: held {c.duration:.2f} s", 3000)

    def _clock(self) -> None:
        eng = self.s.engine
        p = self.s.project
        pos = eng.position()
        self.tc_label.setText(seconds_to_tc(pos, p.frame_rate, p.tc_offset))
        g = p.beat_grid
        if g.downbeats and pos >= g.downbeats[0] - 1e-6:
            bar = bisect.bisect_right(g.downbeats, pos + 1e-6) - 1
            beat = bisect.bisect_right(g.beats, pos + 1e-6) - bisect.bisect_left(g.beats, g.downbeats[bar] - 1e-6)
            self.bar_label.setText(f"Bar {g.bar_number(bar)} · {beat}")
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

    def _set_snap_div(self, d: int) -> None:
        self.s.project.snap_div = int(d or 1)
        self.s._touch()
        self.statusBar().showMessage(f"Snap to {self.snap_div.currentText()}", 3000)

    def _sync_snap_div(self) -> None:
        self.snap_div.setCurrentIndex(max(0, self.snap_div.findData(getattr(self.s.project, "snap_div", 1))))

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

    def _set_scrub(self, v: bool) -> None:
        self.canvas.scrub_audio = v
        self.s.settings.set("scrub_audio", v)

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
        self._show_panel(self.dock_sugs)
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

    def set_auto_advance(self, on: bool) -> None:
        self.s.set_auto_advance(on)

    def _sync_advance(self) -> None:
        self.a_advance.blockSignals(True)
        self.a_advance.setChecked(self.s.auto_advance)
        self.a_advance.blockSignals(False)

    def _advance_after(self, t: float) -> None:
        """After a decision, move straight to the next suggestion for fast review (if on)."""
        if not self.s.auto_advance:
            return
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
    def analyse(self, all_songs: bool = False, song_ids: list[str] | None = None) -> None:
        if self.s.analysis_running():
            self.statusBar().showMessage("An analysis is already running (Cancel in the status bar)", 5000)
            return
        if not any(self.s.analysable(song) for song in self.s.project.songs):
            QMessageBox.information(self, "Analyse", "Import some audio first (File ▸ Import audio).")
            return
        dlg = AnalysisDialog(self.s, self, scope="all" if all_songs else "this", song_ids=song_ids)
        if dlg.exec():
            if not self.s.run_analysis(dlg.options(), dlg.song_ids()):
                QMessageBox.information(self, "Analyse", "Nothing to analyse: the selected song(s) have no "
                                        "track with role Track or Stem.")

    def import_cuepoints(self) -> None:
        from .cuepoints_dialog import CuePointsImportDialog
        path, _ = QFileDialog.getOpenFileName(self, "Import CuePoints CSV / TAB", self._dir(),
                                              "Cue lists (*.csv *.tsv *.txt *.tab);;All files (*)")
        if not path:
            return
        self.s.settings.set("last_dir", os.path.dirname(path))
        try:
            dlg = CuePointsImportDialog(self.s, path, self)
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Import CuePoints", f"Could not read the file:\n{exc}")
            return
        if not dlg.exec():
            return
        items, problems = dlg.items()
        r = self.s.import_cuepoints(items, dlg.options())
        msg = f"Imported {r['cues']} cue(s)."
        if r["songs_created"]:
            msg += f"\nNew songs: {', '.join(r['songs_created'])} (add their audio from the setlist)."
        if r["lanes_created"]:
            msg += f"\nNew lanes: {', '.join(r['lanes_created'])} (set their MA3 sequence in Lanes)."
        if r["skipped"] or problems:
            msg += f"\nSkipped {r['skipped'] + len(problems)} row(s) (duplicates, before the song start or no time)."
        self._notice("Import CuePoints", msg)

    def ai_models(self) -> None:
        from .ai_models_dialog import AIModelsDialog
        AIModelsDialog(self.s.settings, self).exec()

    def reanalyse(self) -> None:
        """Run the analysis again with the last options (no dialog), on the song whose bar 1
        was changed."""
        self.reanalyse_btn.setVisible(False)
        sid = getattr(self, "_rephased_song", None) or self.s.project.song.id
        if self.s.analysis_running() or not self.s.project.song_by_id(sid):
            return
        from ..analysis.pipeline import AnalysisOptions
        prev = self.s.settings.get("analysis_options", {}) or {}
        opts = AnalysisOptions(**{k: v for k, v in prev.items() if k in AnalysisOptions.__dataclass_fields__})
        self.s.run_analysis(opts, [sid])

    def _grid_rephased(self) -> None:
        p = self.s.project
        if p.suggestions and self.s.analysable(p.song):
            self._rephased_song = p.song.id
            self.reanalyse_btn.setText(f"↻ Re-analyse '{p.song.name}' with the new bars")
            self.reanalyse_btn.setVisible(True)
        g = p.beat_grid
        if g.downbeats:
            self.statusBar().showMessage(
                f"Bar 1 at {seconds_to_tc(g.downbeats[g.bar_one_index()], p.frame_rate, p.tc_offset)}"
                " — grid confirmed (re-analysis keeps it)", 6000)

    def _analysis_done(self, res) -> None:
        # non-modal: analysis may finish while you're busy programming, or after a long bulk run
        if isinstance(res, str):
            self._notice("Analysis failed", res[:2000], warn=True)
            return
        self._show_panel(self.dock_sugs)
        n = len(self.s.project.visible_suggestions())
        msg = "\n".join(res.log[-12:])
        self.statusBar().showMessage(f"Analysis done — {n} suggestions visible in this song. Tab to review.", 10000)
        if res.grid and not self.s.project.beat_grid.confirmed:
            msg += ("\n\nThe detected beat grid is dashed until you accept it (AI Suggestions ▸ Accept grid). "
                    "Wrong bar 1? Press D on the 'one' while playing, or Grid ▸ Move bar 1.")
        self._notice("Analysis done", msg)

    def _notice(self, title: str, text: str, warn: bool = False) -> None:
        box = QMessageBox(QMessageBox.Warning if warn else QMessageBox.Information, title, text,
                          QMessageBox.Ok, self)
        box.setWindowModality(Qt.NonModal)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setAttribute(Qt.WA_ShowWithoutActivating)    # keep the keyboard on the timeline
        box.show()
        self.activateWindow()
        self._last_notice = box

    def _tap_grid_mode(self, on: bool) -> None:
        if on:
            self.s.start_tap_grid()
            if not self.s.engine.playing:
                self.s.engine.play()
        else:
            self.s.finish_tap_grid()

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
        except Exception as exc:              # never fail silently: the title would stay "Untitled"
            QMessageBox.warning(self, "Save failed", f"{type(exc).__name__}: {exc}")
            return False
        finally:
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

    def import_ma3(self, paths: list[str] | None = None, replace: bool | None = None) -> None:
        """Round trip: bring timecode shows edited on the console back in. Each file goes to
        the song with the same name (or the current song when importing one file)."""
        from ..export.ma3_import import import_into_song, parse_timecode_xml
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(self, "Import grandMA3 timecode XML", self._dir(),
                                                    "grandMA3 XML (*.xml)")
        if not paths:
            return
        if replace is None:
            box = QMessageBox(self)
            box.setWindowTitle("Import timecode")
            box.setText("Replace the cues in the matching lanes with the console's version, or merge?")
            rep = box.addButton("Replace (console is master)", QMessageBox.AcceptRole)
            box.addButton("Merge", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is None or box.clickedButton() == box.button(QMessageBox.Cancel):
                return
            replace = box.clickedButton() is rep
        p = self.s.project
        report = []
        for path in paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    show = parse_timecode_xml(fh.read())
            except Exception as exc:
                report.append(f"{os.path.basename(path)}: could not read ({exc})")
                continue
            target = next((x for x in p.songs if x.name.lower() == show.name.lower()), None)
            if target is None and len(paths) > 1:
                target = p.add_song(show.name)
            target = target or p.song
            self.s.switch_song(target.id)
            with self.s.edit("Import timecode"):
                info = import_into_song(p, show, replace=replace)
            self.s.lanes_changed.emit()
            report.append(f"{show.name} → '{target.name}': {info['cues']} cues"
                          + (f", new lanes: {', '.join(info['lanes_created'])}" if info["lanes_created"] else ""))
        self.s.songs_changed.emit()
        QMessageBox.information(self, "Imported", "\n".join(report))

    def link_settings(self) -> None:
        from .link_dialog import LinkDialog
        LinkDialog(self.ma3link, self).exec()
        self._link_badge()

    def _link_badge(self) -> None:
        on = self.ma3link.active
        self.link_label.setText("● MA3 link" if on else "")
        self.link_label.setToolTip(f"Sending to {self.ma3link.cfg.host}:{self.ma3link.cfg.port}" if on else "")

    def control_settings(self) -> None:
        from .control_dialog import ControlDialog
        ControlDialog(self.control, self).exec()

    def audio_device(self) -> None:
        AudioDeviceDialog(self.s, self).exec()

    # ================================================================ export
    def _export_path(self, title: str, suffix: str, filt: str) -> str:
        default = os.path.join(self._dir(), self.s.project.song.name + suffix)
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
        from ..export.ma3 import (build_ma3_macro_commands, export_ma3_lua, export_ma3_xml, export_ma3_xml_all,
                                  songs_with_cues)
        p = self.s.project
        if not songs_with_cues(p):
            QMessageBox.information(self, "Export", "No cues to export yet.")
            return
        if not self._check_pending():
            return
        dlg = MA3ExportDialog(self.s, self)
        if not dlg.exec():
            return
        fmt = self.s.settings.get("ma3_format", "lua")
        all_songs = getattr(dlg, "all_songs", False)
        try:
            if fmt == "xml" and all_songs:
                folder = QFileDialog.getExistingDirectory(self, "Folder for the timecode XML files", self._dir())
                if folder:
                    paths = export_ma3_xml_all(p, folder)
                    self._exported(paths[0] if paths else folder,
                                   f"{len(paths)} timecode files written (one per song):\n"
                                   + "\n".join(os.path.basename(x) for x in paths)
                                   + "\n\nCopy them to gma3_library/datapools/timecodes and import each into "
                                     "its Timecode pool slot.")
            elif fmt == "xml":
                path = self._export_path("Export grandMA3 timecode XML", ".xml", "XML (*.xml)")
                if path:
                    n = export_ma3_xml(p, path)
                    self._exported(path, f"{n} cue events written for '{p.song.name}'.\n\nOn the console: copy "
                                         "the file to gma3_library/datapools/timecodes (USB stick or onPC library "
                                         f"folder), then import it into Timecode {p.song.ma3_timecode}.")
            elif fmt == "lua":
                path = self._export_path("Export grandMA3 Lua plugin", ".lua", "Lua (*.lua)")
                if path:
                    xml_path = export_ma3_lua(p, path, None, dlg.create.isChecked(), all_songs)
                    which = f"all {len(p.songs)} songs, each into its own Timecode slot" if all_songs else \
                        f"'{p.song.name}' into Timecode {p.song.ma3_timecode}"
                    self._exported(path, f"Also wrote {os.path.basename(xml_path)} (plugin descriptor).\n\n"
                                         "Copy both files to gma3_library/datapools/plugins, import the plugin "
                                         "into a Plugin pool slot and run it once. It creates the cues and "
                                         f"imports the timecode for {which}. No separate XML import needed.")
            else:
                path = self._export_path("Export command list", ".txt", "Text (*.txt)")
                if path:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("\n".join(build_ma3_macro_commands(p, all_songs)) + "\n")
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
                all_songs = len(self.s.project.songs) > 1 and QMessageBox.question(
                    self, "CSV", "Export the whole setlist? (No = current song only)") == QMessageBox.Yes
                n = export_csv(self.s.project, path, all_songs=all_songs)
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
        self.s.cancel_analysis()               # stop the background worker (and clean its folder)
        self.s.engine.close()
        self.control.close()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().removeEventFilter(self)
        from .workers import wait_all
        wait_all(2000)
        dual = self.panels_window is not None
        if dual:
            self.dual_monitor(False)            # docks back home so the layout saves cleanly
            self.s.settings.set("dual_monitor", True)
        self.s.settings.set("geometry", self.saveGeometry().toHex().data().decode())
        self.s.settings.set("state", self.saveState().toHex().data().decode())
        e.accept()

    def reset_layout(self) -> None:
        if self.panels_window is not None:
            self.dual_monitor(False)
        for d in self._docks():
            d.setFloating(False)
            d.show()
        self.addDockWidget(Qt.LeftDockWidgetArea, self.dock_setlist)
        for d in (self.dock_sugs, self.dock_cues, self.dock_lanes):
            self.addDockWidget(Qt.RightDockWidgetArea, d)
        self.tabifyDockWidget(self.dock_sugs, self.dock_cues)
        self.tabifyDockWidget(self.dock_cues, self.dock_lanes)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dock_mixer)
        self.dock_sugs.raise_()

    def restore_layout(self) -> None:
        from PySide6.QtCore import QByteArray
        g, st = self.s.settings.get("geometry"), self.s.settings.get("state")
        if g:
            self.restoreGeometry(QByteArray.fromHex(g.encode()))
        if st:
            self.restoreState(QByteArray.fromHex(st.encode()))
        QTimer.singleShot(0, self.control.apply)   # open configured MIDI / OSC
        QTimer.singleShot(0, lambda: (self.ma3link.apply(), self._link_badge()))
        from PySide6.QtGui import QGuiApplication
        if self.s.settings.get("dual_monitor") and len(QGuiApplication.screens()) > 1:
            QTimer.singleShot(300, lambda: self.dual_monitor(True))
        QTimer.singleShot(0, self.retab_inspector)     # layouts saved by older versions
