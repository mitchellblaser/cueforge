"""Side panels: cue list, AI suggestion review, lane setup."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
                               QVBoxLayout, QWidget, QLineEdit)

from ..core.model import KIND_LABELS, SUGGESTION_KINDS
from ..core.timecode import parse_tc, seconds_to_tc
from . import theme
from .session import RESERVED_KEYS, Session


# ============================================================== cue list
class CueTable(QWidget):
    seek_requested = Signal(float)
    COLS = ["Timecode", "Lane", "Cue", "Label", "Fade", "Temp", "Notes", "Src"]

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self._syncing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.lane_filter = QComboBox()
        self.lane_filter.currentIndexChanged.connect(self.rebuild)
        top.addWidget(QLabel("Lane:"))
        top.addWidget(self.lane_filter, 1)
        self.count = QLabel()
        self.count.setStyleSheet(f"color: {theme.FG_DIM};")
        top.addWidget(self.count)
        self.follow = QCheckBox("Follow")
        self.follow.setToolTip("While playing, highlight the cue each lane is on and scroll to the cue "
                               "that just fired")
        self.follow.setChecked(bool(session.settings.get("cue_list_follow", True)))
        self.follow.toggled.connect(lambda v: (session.settings.set("cue_list_follow", v), self._light(force=True)))
        top.addWidget(self.follow)
        lay.addLayout(top)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        for i, w in enumerate([92, 80, 44, 120, 40, 40, 80, 28]):
            if i != 3:
                self.table.setColumnWidth(i, w)
        self.table.itemSelectionChanged.connect(self._sel_from_table)
        self.table.itemChanged.connect(self._edited)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.table)
        self._rows: dict[str, int] = {}
        self._lit: dict[str, str] = {}          # cue id -> "on" | "fired"
        self._user_scrolled = 0.0
        self._last_pos = -1.0
        self.table.verticalScrollBar().sliderPressed.connect(self._scrolled_by_user)
        self.table.viewport().installEventFilter(self)
        self._tick = QTimer(self)
        self._tick.setInterval(60)
        self._tick.timeout.connect(self._light)
        self._tick.start()
        hint = QLabel("Double-click a cell to edit · right-click for more")
        hint.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        lay.addWidget(hint)

        session.cues_changed.connect(self.rebuild)
        session.lanes_changed.connect(self._lanes)
        session.project_replaced.connect(self._lanes)
        session.selection_changed.connect(self._sel_from_session)
        self._lanes()

    def _lanes(self) -> None:
        cur = self.lane_filter.currentData()
        self.lane_filter.blockSignals(True)
        self.lane_filter.clear()
        self.lane_filter.addItem("All lanes", "")
        for l in self.s.project.lanes:
            self.lane_filter.addItem(l.name, l.id)
        i = self.lane_filter.findData(cur)
        self.lane_filter.setCurrentIndex(max(0, i))
        self.lane_filter.blockSignals(False)
        self.rebuild()

    def rebuild(self) -> None:
        p = self.s.project
        lane_id = self.lane_filter.currentData()
        cues = [c for c in p.cues if not lane_id or c.lane_id == lane_id]
        self._syncing = True
        self.table.setRowCount(len(cues))
        for r, c in enumerate(cues):
            lane = p.lane(c.lane_id)
            vals = [seconds_to_tc(c.time, p.frame_rate, p.tc_offset), lane.name if lane else "?",
                    "" if c.number is None else f"{c.number:g}", c.label,
                    "" if c.fade is None else f"{c.fade:g}", "" if not c.duration else f"{c.duration:.2f}",
                    c.notes, "AI" if c.source == "ai-accepted" else ""]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, c.id)
                if col in (1, 7):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if col == 1 and lane:
                    it.setForeground(QColor(lane.color))
                if col == 7:
                    it.setForeground(QColor("#ffd54f"))
                if col == 6 and c.notes:
                    it.setToolTip(c.notes)
                self.table.setItem(r, col, it)
        self.count.setText(f"{len(cues)} cues")
        self._rows = {c.id: r for r, c in enumerate(cues)}
        self._lit = {}
        self._syncing = False
        self._sel_from_session()
        self._light(force=True)

    # -- follow the playhead ------------------------------------------------
    def eventFilter(self, obj, ev) -> bool:
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.Wheel:
            self._scrolled_by_user()
        return False

    def _scrolled_by_user(self) -> None:
        self._user_scrolled = time.monotonic()

    def active_cues(self, t: float) -> tuple[dict[str, str], str | None]:
        """{cue id: "fired" | "on"} at time t, and the cue that fired last. "on" is the cue
        each lane is sitting on (and Temps still holding); "fired" is a cue that went in the
        last 0.4 s."""
        p = self.s.project
        state: dict[str, str] = {}
        latest = None
        for lane in p.lanes:
            cues = p.cues_in_lane(lane.id)
            before = [c for c in cues if c.time <= t + 1e-6]
            plain = [c for c in before if not c.duration]
            if plain:
                state[plain[-1].id] = "on"
            for c in before:
                if c.duration and c.time + c.duration > t:
                    state[c.id] = "on"
            for c in before[-3:]:
                if t - c.time < 0.4:
                    state[c.id] = "fired"
            if before and (latest is None or before[-1].time > latest.time):
                latest = before[-1]
        return state, latest.id if latest else None

    def _light(self, force: bool = False) -> None:
        eng = self.s.engine
        playing = bool(getattr(eng, "playing", False))
        if not self.follow.isChecked() or not self._rows or self.table.state() == QAbstractItemView.EditingState:
            state, latest = {}, None
        elif not playing and not force and (not self._lit or eng.position() == self._last_pos):
            return                              # stopped and nothing moved: nothing to do
        else:
            state, latest = self.active_cues(eng.position())
        self._last_pos = eng.position()
        state = {k: v for k, v in state.items() if k in self._rows}
        if state == self._lit and not force:
            return
        p = self.s.project
        was, self._syncing = self._syncing, True      # styling emits itemChanged: not an edit
        for cid in set(self._lit) | set(state):
            r = self._rows.get(cid)
            if r is None:
                continue
            c = p.cue(cid)
            lane = p.lane(c.lane_id) if c else None
            mode = state.get(cid)
            if mode:
                col = QColor(lane.color if lane else theme.ACCENT)
                col.setAlpha(150 if mode == "fired" else 60)
                brush = QBrush(col)
            else:
                brush = QBrush()
            for k in range(self.table.columnCount()):
                it = self.table.item(r, k)
                if it is None:
                    continue
                it.setBackground(brush)
                f = it.font()
                f.setBold(mode == "fired")
                it.setFont(f)
        self._syncing = was
        self._lit = state
        if playing and latest in self._rows and latest in state and state[latest] == "fired" \
                and time.monotonic() - self._user_scrolled > 3.0:
            self.table.scrollToItem(self.table.item(self._rows[latest], 0), QAbstractItemView.PositionAtCenter)

    def _sel_from_session(self) -> None:
        self._syncing = True
        self.table.clearSelection()
        mode = self.table.selectionMode()
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        first = None
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.UserRole) in self.s.sel_cues:
                self.table.selectRow(r)
                first = first if first is not None else r
        self.table.setSelectionMode(mode)
        if first is not None:
            self.table.scrollToItem(self.table.item(first, 0))
        self._syncing = False

    def _sel_from_table(self) -> None:
        if self._syncing:
            return
        ids = {self.table.item(i.row(), 0).data(Qt.UserRole) for i in self.table.selectionModel().selectedRows()}
        self._syncing = True
        self.s.select(cues=ids)
        self._syncing = False
        if len(ids) == 1:
            c = self.s.project.cue(next(iter(ids)))
            if c:
                self.seek_requested.emit(c.time)

    def _edited(self, it: QTableWidgetItem) -> None:
        if self._syncing:
            return
        cid = it.data(Qt.UserRole)
        col = it.column()
        txt = it.text().strip()
        p = self.s.project
        try:
            if col == 0:
                self.s.update_cue(cid, time=max(0.0, parse_tc(txt, p.frame_rate, p.tc_offset)))
            elif col == 2:
                self.s.update_cue(cid, number=float(txt) if txt else None)
            elif col == 3:
                self.s.update_cue(cid, label=txt)
            elif col == 4:
                self.s.update_cue(cid, fade=float(txt) if txt else None)
            elif col == 5:
                self.s.update_cue(cid, duration=float(txt) if txt and float(txt) > 0 else None)
            elif col == 6:
                self.s.update_cue(cid, notes=txt)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid value", str(exc))
            self.rebuild()

    def _menu(self, pos) -> None:
        if not self.s.sel_cues:
            return
        m = QMenu(self)
        m.addAction("Delete", self.s.delete_selected)
        m.addAction("Snap to grid", self.s.snap_selected)
        sub = m.addMenu("Move to lane")
        for lane in self.s.project.lanes:
            sub.addAction(lane.name, lambda lid=lane.id: self.s.move_selected_to_lane(lid))
        m.addAction("Clear cue numbers (auto)", self._clear_numbers)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _clear_numbers(self) -> None:
        with self.s.edit("Clear numbers"):
            for c in self.s.project.cues:
                if c.id in self.s.sel_cues:
                    c.number = None


# ============================================================== suggestions
class KindRow(QWidget):
    def __init__(self, panel: "SuggestionPanel", kind: str) -> None:
        super().__init__()
        self.panel, self.kind, s = panel, kind, panel.s
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setHorizontalSpacing(6)
        self.visible = QCheckBox(KIND_LABELS[kind])
        self.visible.setStyleSheet("font-weight: bold;")
        self.visible.toggled.connect(lambda v: s.set_filter(kind, visible=v))
        self.count = QLabel()
        self.count.setStyleSheet(f"color: {theme.FG_DIM};")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setToolTip("Minimum confidence shown")
        self.slider.valueChanged.connect(self._thr)
        self.thr_label = QLabel()
        self.thr_label.setFixedWidth(34)
        self.lane = QComboBox()
        self.lane.setToolTip("Lane that accepted suggestions go into")
        self.lane.activated.connect(lambda _: s.set_kind_lane(kind, self.lane.currentData()))
        acc = QPushButton("✓ all")
        acc.setToolTip("Accept every visible suggestion of this type")
        acc.clicked.connect(self._accept_all)
        rej = QPushButton("✗ all")
        rej.setToolTip("Reject every visible suggestion of this type")
        rej.clicked.connect(lambda: s.reject(s.visible_ids(kind)))
        for b in (acc, rej):
            b.setStyleSheet("padding: 2px 6px;")
        self.visible.setMinimumWidth(110)
        lay.addWidget(self.visible, 0, 0)
        lay.addWidget(self.count, 0, 1, 1, 2)
        lay.addWidget(QLabel("→"), 0, 3)
        lay.addWidget(self.lane, 0, 4, 1, 2)
        lay.addWidget(self.slider, 1, 0, 1, 2)
        lay.addWidget(self.thr_label, 1, 2)
        lay.addWidget(acc, 1, 4)
        lay.addWidget(rej, 1, 5)

    def _thr(self, v: int) -> None:
        self.thr_label.setText(f"{v}%")
        if not self.panel._refreshing:
            self.panel.s.set_filter(self.kind, threshold=v / 100)

    def _accept_all(self) -> None:
        ids = self.panel.s.visible_ids(self.kind)
        if len(ids) > 20 and QMessageBox.question(
                self, "Accept all", f"Accept {len(ids)} {KIND_LABELS[self.kind].lower()} suggestions?") \
                != QMessageBox.Yes:
            return
        self.panel.s.accept(ids)

    def refresh(self) -> None:
        s = self.panel.s
        a = s.project.analysis
        pending = [x for x in s.project.suggestions if x.kind == self.kind and x.status == "pending"]
        shown = [x for x in pending if x.confidence >= a.thresholds.get(self.kind, 0)]
        self.visible.blockSignals(True)
        self.visible.setChecked(a.visible.get(self.kind, True))
        self.visible.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(a.thresholds.get(self.kind, 0) * 100)))
        self.slider.blockSignals(False)
        self.thr_label.setText(f"{self.slider.value()}%")
        self.count.setText(f"{len(shown)} / {len(pending)}")
        self.count.setToolTip(f"{len(shown)} shown at this threshold, {len(pending)} pending in total")
        self.lane.clear()
        for l in s.project.lanes:
            self.lane.addItem(l.name, l.id)
        lid = s.project.analysis.lane_for_kind.get(self.kind) or s.project.lane_for_kind(self.kind)
        self.lane.setCurrentIndex(max(0, self.lane.findData(lid)))


class SuggestionPanel(QWidget):
    analyse_requested = Signal()
    tempo_requested = Signal()
    seek_requested = Signal(float)

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self._refreshing = False
        self._syncing = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        split = QSplitter(Qt.Vertical)
        outer.addWidget(split)
        controls = QWidget()
        lay = QVBoxLayout(controls)
        lay.setContentsMargins(0, 0, 0, 0)
        split.addWidget(controls)

        top = QHBoxLayout()
        self.analyse_btn = QPushButton("✦ Analyse audio…")
        self.analyse_btn.setStyleSheet(f"font-weight: bold; padding: 6px; border-color: {theme.ACCENT};")
        self.analyse_btn.clicked.connect(self.analyse_requested.emit)
        top.addWidget(self.analyse_btn)
        lay.addLayout(top)
        note = QLabel("AI suggestions are drafts. Nothing becomes a cue until you accept it.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        lay.addWidget(note)

        grid_box = QGroupBox("Beat grid")
        gl = QVBoxLayout(grid_box)
        self.grid_label = QLabel()
        self.grid_label.setWordWrap(True)
        gl.addWidget(self.grid_label)
        gb = QHBoxLayout()
        self.grid_accept = QPushButton("Accept grid")
        self.grid_accept.clicked.connect(session.accept_grid)
        tempo = QPushButton("Set tempo…")
        tempo.clicked.connect(self.tempo_requested.emit)
        clear = QPushButton("Clear")
        clear.clicked.connect(session.clear_grid)
        gb.addWidget(self.grid_accept)
        gb.addWidget(tempo)
        gb.addWidget(clear)
        gl.addLayout(gb)
        lay.addWidget(grid_box)

        kinds_box = QGroupBox("Show suggestions (filters)")
        kl = QVBoxLayout(kinds_box)
        fnote = QLabel("Unticking a type or raising its threshold only <b>hides</b> suggestions. To get rid of "
                       "them, press <b>✗ all</b> or <b>Reject hidden</b> below.")
        fnote.setWordWrap(True)
        fnote.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        kl.addWidget(fnote)
        rows_w = QWidget()
        rows_l = QVBoxLayout(rows_w)
        rows_l.setContentsMargins(0, 0, 0, 0)
        self.rows = {k: KindRow(self, k) for k in SUGGESTION_KINDS}
        for r in self.rows.values():
            rows_l.addWidget(r)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(rows_w)
        scroll.setMinimumHeight(160)
        kl.addWidget(scroll, 1)
        self.learned = QLabel()
        self.learned.setWordWrap(True)
        self.learned.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        self.apply_learned = QPushButton("Apply learned thresholds")
        self.apply_learned.clicked.connect(self._apply_learned)
        kl.addWidget(self.learned)
        brow = QHBoxLayout()
        brow.addWidget(self.apply_learned)
        self.reject_hidden = QPushButton("Reject hidden")
        self.reject_hidden.setToolTip("Reject every pending suggestion the filters hide in this song, so they "
                                      "stop counting as 'to review' (undo brings them back)")
        self.reject_hidden.clicked.connect(self._reject_hidden)
        brow.addWidget(self.reject_hidden)
        kl.addLayout(brow)
        lay.addWidget(kinds_box)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Time", "Type", "Label", "Conf", "Idea", "Why"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        for i, w in enumerate([86, 70, 110, 40, 150]):
            self.tree.setColumnWidth(i, w)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.itemSelectionChanged.connect(self._sel_from_tree)
        self.tree.itemDoubleClicked.connect(lambda it, _: session.accept([it.data(0, Qt.UserRole)]))
        bottom = QWidget()
        lay = QVBoxLayout(bottom)
        lay.setContentsMargins(0, 0, 0, 0)
        split.addWidget(bottom)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        lay.addWidget(self.tree, 1)
        b = QHBoxLayout()
        acc = QPushButton("Accept (A)")
        acc.clicked.connect(lambda: session.accept(set(session.sel_sugs)))
        rej = QPushButton("Reject (X)")
        rej.clicked.connect(lambda: session.reject(set(session.sel_sugs)))
        b.addWidget(acc)
        b.addWidget(rej)
        lay.addLayout(b)
        tip = QLabel("Tab / Shift+Tab: next / previous suggestion")
        tip.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        lay.addWidget(tip)

        session.cues_changed.connect(self.refresh)
        session.lanes_changed.connect(self.refresh)
        session.project_replaced.connect(self.refresh)
        session.selection_changed.connect(self._sel_from_session)
        session.busy.connect(lambda m: self.analyse_btn.setEnabled(not m.startswith("Analys")))
        self.refresh()

    def refresh(self) -> None:
        self._refreshing = True
        s = self.s
        for r in self.rows.values():
            r.refresh()
        g = s.project.beat_grid
        if g.beats:
            state = "confirmed" if g.confirmed else "<span style='color:#ffb74d'>unconfirmed suggestion</span>"
            self.grid_label.setText(f"{g.bpm():.2f} BPM · {g.beats_per_bar}/4 · from {g.source or '?'}"
                                    f" · {state}" + (f" · confidence {g.confidence:.0%}" if not g.confirmed else ""))
        else:
            self.grid_label.setText("No grid. Analyse, load a click track, or set the tempo.")
        self.grid_accept.setEnabled(bool(g.beats) and not g.confirmed)
        learned = s.learned_thresholds()
        if learned:
            parts = [f"{KIND_LABELS[k]} ≥ {v['threshold']:.0%} (from {v['n']} decisions)" for k, v in learned.items()]
            self.learned.setText("Learned from your accept/reject history: " + "; ".join(parts))
            self.apply_learned.setVisible(True)
        else:
            self.learned.setText("Thresholds will be learned from your accept/reject decisions over time.")
            self.apply_learned.setVisible(False)

        p = s.project
        self._syncing = True
        self.tree.clear()
        for sg in p.visible_suggestions():
            it = QTreeWidgetItem([seconds_to_tc(sg.time, p.frame_rate, p.tc_offset), KIND_LABELS[sg.kind],
                                  sg.label, f"{sg.confidence:.0%}", sg.idea, sg.reason])
            it.setData(0, Qt.UserRole, sg.id)
            it.setToolTip(4, sg.idea)
            it.setToolTip(5, sg.reason)
            it.setSelected(sg.id in s.sel_sugs)
            self.tree.addTopLevelItem(it)
        self._syncing = False
        self._refreshing = False

    def _tree_menu(self, pos) -> None:
        it = self.tree.itemAt(pos)
        if not it:
            return
        sid = it.data(0, Qt.UserRole)
        sg = self.s.project.suggestion(sid)
        m = QMenu(self)
        m.addAction("Accept", lambda: self.s.accept([sid]))
        if sg and sg.steps:
            m.addAction(f"Accept as chase steps ({len(sg.steps)} cues)", lambda: self.s.accept_as_steps(sid))
        m.addAction("Reject", lambda: self.s.reject([sid]))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _hidden_ids(self) -> list[str]:
        p = self.s.project
        vis = {x.id for x in p.visible_suggestions()}
        return [x.id for x in p.suggestions if x.status == "pending" and x.id not in vis]

    def _reject_hidden(self) -> None:
        ids = self._hidden_ids()
        if ids and QMessageBox.question(self, "Reject hidden suggestions",
                                        f"Reject the {len(ids)} suggestion(s) the filters hide in this song?") \
                == QMessageBox.Yes:
            self.s.reject(ids)

    def _apply_learned(self) -> None:
        for k, v in self.s.learned_thresholds().items():
            self.s.set_filter(k, threshold=v["threshold"])
        self.refresh()

    def _sel_from_session(self) -> None:
        self._syncing = True
        first = None
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            sel = it.data(0, Qt.UserRole) in self.s.sel_sugs
            it.setSelected(sel)
            if sel and first is None:
                first = it
        if first:
            self.tree.scrollToItem(first)
        self._syncing = False

    def _sel_from_tree(self) -> None:
        if self._syncing:
            return
        ids = {it.data(0, Qt.UserRole) for it in self.tree.selectedItems()}
        self.s.select(sugs=ids)
        if len(ids) == 1:
            sg = self.s.project.suggestion(next(iter(ids)))
            if sg:
                self.seek_requested.emit(sg.time)


# ============================================================== lanes
class LanePanel(QWidget):
    COLS = ["Name", "Colour", "Tap key", "MA3 seq", "Export"]

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self._syncing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i, w in enumerate([0, 56, 56, 64, 50]):
            if w:
                self.table.setColumnWidth(i, w)
        self.table.cellDoubleClicked.connect(self._dbl)
        self.table.itemChanged.connect(self._renamed)
        lay.addWidget(self.table)
        b = QGridLayout()
        for i, (txt, fn) in enumerate([("Add lane", self._add), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                                       ("Down", lambda: self._move(1)), ("Renumber cues…", self._renumber)]):
            btn = QPushButton(txt)
            btn.clicked.connect(fn)
            b.addWidget(btn, i // 3, i % 3)
        lay.addLayout(b)
        help_ = QLabel("Each lane exports to one grandMA3 sequence as a timecode track.\n"
                       "Press a lane's tap key during playback to drop a cue in it.")
        help_.setWordWrap(True)
        help_.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        lay.addWidget(help_)
        lay.addStretch()
        session.lanes_changed.connect(self.rebuild)
        session.project_replaced.connect(self.rebuild)
        self.rebuild()

    def _lane_id(self, row: int | None = None) -> str | None:
        row = self.table.currentRow() if row is None else row
        it = self.table.item(row, 0) if row >= 0 else None
        return it.data(Qt.UserRole) if it else None

    def rebuild(self) -> None:
        self._syncing = True
        cur = self._lane_id()
        lanes = self.s.project.lanes
        self.table.setRowCount(len(lanes))
        for r, l in enumerate(lanes):
            name = QTableWidgetItem(l.name)
            name.setData(Qt.UserRole, l.id)
            self.table.setItem(r, 0, name)
            col = QTableWidgetItem("")
            col.setBackground(QColor(l.color))
            col.setFlags(col.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 1, col)
            key = QLineEdit(l.tap_key.upper())
            key.setMaxLength(1)
            key.setAlignment(Qt.AlignCenter)
            key.editingFinished.connect(lambda lid=l.id, w=key: self._key(lid, w.text()))
            self.table.setCellWidget(r, 2, key)
            seq = QSpinBox()
            seq.setRange(1, 9999)
            seq.setValue(l.ma3_sequence)
            seq.editingFinished.connect(lambda lid=l.id, w=seq: self.s.update_lane(lid, ma3_sequence=w.value())
                                        if self.s.project.lane(lid).ma3_sequence != w.value() else None)
            self.table.setCellWidget(r, 3, seq)
            ex = QCheckBox()
            ex.setChecked(l.export)
            ex.toggled.connect(lambda v, lid=l.id: self.s.update_lane(lid, export=v))
            self.table.setCellWidget(r, 4, ex)
            if l.id == cur:
                self.table.selectRow(r)
        self._syncing = False

    def _renamed(self, it: QTableWidgetItem) -> None:
        if self._syncing or it.column() != 0:
            return
        lid = it.data(Qt.UserRole)
        if lid and it.text().strip() and self.s.project.lane(lid).name != it.text().strip():
            self.s.update_lane(lid, name=it.text().strip())

    def _key(self, lid: str, text: str) -> None:
        text = text.strip().lower()[:1]
        lane = self.s.project.lane(lid)
        if not lane or lane.tap_key == text:
            return
        if text in RESERVED_KEYS or text == " ":
            QMessageBox.warning(self, "Tap key", f"'{text.upper()}' is used by another shortcut. Use 1–9 or another letter.")
            self.rebuild()
            return
        for other in self.s.project.lanes:
            if other.id != lid and other.tap_key == text and text:
                self.s.update_lane(other.id, tap_key="")
        self.s.update_lane(lid, tap_key=text)

    def _dbl(self, row: int, col: int) -> None:
        if col == 1:
            lid = self._lane_id(row)
            c = QColorDialog.getColor(QColor(self.s.project.lane(lid).color), self, "Lane colour")
            if c.isValid():
                self.s.update_lane(lid, color=c.name())

    def _add(self) -> None:
        self.s.add_lane()

    def _remove(self) -> None:
        lid = self._lane_id()
        if not lid:
            return
        n = len(self.s.project.cues_in_lane(lid))
        if n and QMessageBox.question(self, "Remove lane", f"Remove lane and its {n} cue(s)?") != QMessageBox.Yes:
            return
        self.s.remove_lane(lid)

    def _move(self, d: int) -> None:
        lid = self._lane_id()
        if lid:
            self.s.move_lane(lid, d)

    def _renumber(self) -> None:
        lid = self._lane_id()
        if not lid:
            QMessageBox.information(self, "Renumber", "Select a lane first.")
            return
        start, ok = QInputDialog.getDouble(self, "Renumber cues", "First cue number:", 1, 0, 99999, 3)
        if not ok:
            return
        step, ok = QInputDialog.getDouble(self, "Renumber cues", "Step:", 1, 0.001, 1000, 3)
        if ok:
            self.s.renumber_lane(lid, start, step)


# ============================================================== setlist
class SongList(QWidget):
    """Left sidebar: the project's setlist. Click a song to open it on the timeline."""
    open_settings = Signal(str)
    add_requested = Signal()

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self._syncing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.list = _SongListWidget(self)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setAlternatingRowColors(True)
        self.list.setSpacing(2)
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setStyleSheet("QListWidget::item { padding: 4px 6px; border-bottom: 1px solid #2a2e36; }"
                                f"QListWidget::item:selected {{ background: #2f5d73; }}")
        self.list.currentItemChanged.connect(self._picked)
        self.list.itemDoubleClicked.connect(lambda it: self.open_settings.emit(it.data(Qt.UserRole)))
        self.list.model().rowsMoved.connect(self._reordered)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for txt, tip, fn in (("＋", "Add a song (choose its audio files)", self.add_requested.emit),
                             ("－", "Remove the selected song", self._remove),
                             ("↑", "Move up", lambda: self._move(-1)), ("↓", "Move down", lambda: self._move(1)),
                             ("⚙", "Song settings: name, start timecode, MA3 timecode slot, cue numbers",
                              lambda: self.open_settings.emit(self._sid()) if self._sid() else None)):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.setFixedWidth(34)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        hint = QLabel("Drop audio files here to add a song.\nCtrl+PgUp / PgDn: previous / next song")
        hint.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        lay.addWidget(hint)
        for sig in (session.songs_changed, session.project_replaced, session.cues_changed):
            sig.connect(self.rebuild)
        self.rebuild()

    def _sid(self) -> str | None:
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def rebuild(self) -> None:
        from ..core.timecode import format_seconds
        p = self.s.project
        self._syncing = True
        self.list.clear()
        for i, song in enumerate(p.songs, 1):
            n_cues = len(song.cues)
            a = p.analysis
            allp = [x for x in song.suggestions if x.status == "pending"]
            pend = sum(1 for x in allp if a.visible.get(x.kind, True) and x.confidence >= a.thresholds.get(x.kind, 0.0))
            hidden = len(allp) - pend
            dur = max((self.s.audio[t.id].duration + t.offset for t in song.tracks if t.id in self.s.audio),
                      default=0.0)
            sub = f"{seconds_to_tc(0, p.frame_rate, song.tc_offset)}   TC {song.ma3_timecode}"
            if dur:
                sub += f"   {format_seconds(dur).split('.')[0]}"
            sub2 = f"{n_cues} cue{'s' if n_cues != 1 else ''}" + (f" · {pend} AI to review" if pend else "")
            it = QListWidgetItem(f"{i}.  {song.name}\n{sub}\n{sub2}")
            it.setData(Qt.UserRole, song.id)
            it.setToolTip(f"{song.name}\nStarts at {seconds_to_tc(0, p.frame_rate, song.tc_offset)}\n"
                          f"grandMA3 Timecode {song.ma3_timecode}, cues from {song.cue_start:g}"
                          + (f", sequences +{song.seq_offset}" if song.seq_offset else "")
                          + (f"\n{song.notes}" if song.notes else "")
                          + (f"\n{hidden} more suggestion(s) hidden by the Suggestion filters "
                             "(not rejected)" if hidden else ""))
            if song.id == p.song.id:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                it.setForeground(QColor(theme.ACCENT))
            self.list.addItem(it)
            if song.id == p.song.id:
                self.list.setCurrentItem(it)
        self._syncing = False

    def _picked(self, cur, prev) -> None:
        if self._syncing or cur is None:
            return
        self.s.switch_song(cur.data(Qt.UserRole))

    def _reordered(self, *args) -> None:
        if self._syncing:
            return
        ids = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self.s.reorder_songs(ids))  # not while the view is mid-move

    def _remove(self) -> None:
        sid = self._sid()
        song = self.s.project.song_by_id(sid) if sid else None
        if not song:
            return
        if QMessageBox.question(self, "Remove song", f"Remove '{song.name}' and its {len(song.cues)} cue(s) "
                                "from the project? (The audio files are not deleted.)") != QMessageBox.Yes:
            return
        self.s.remove_song(sid)

    def _move(self, d: int) -> None:
        sid = self._sid()
        if sid:
            self.s.move_song(sid, d)

    def _menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        m = QMenu(self)
        if it:
            sid = it.data(Qt.UserRole)
            m.addAction("Song settings…", lambda: self.open_settings.emit(sid))
            m.addAction("Remove song", self._remove)
            m.addSeparator()
        m.addAction("Add song…", self.add_requested.emit)
        m.addAction("Auto-number setlist (01:00:00:00, 02:00:00:00 …; TC slots 1…; cues 1, 101, 201 …)",
                    self.s.auto_number_setlist)
        m.exec(self.list.viewport().mapToGlobal(pos))


class _SongListWidget(QListWidget):
    """Accepts audio files dropped from the file manager (creates a new song)."""

    def __init__(self, panel: SongList) -> None:
        super().__init__()
        self.panel = panel
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            import os
            from ..audio.loader import AUDIO_EXTENSIONS
            paths = [u.toLocalFile() for u in e.mimeData().urls()
                     if u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower() in AUDIO_EXTENSIONS]
            if paths:
                self.panel.s.add_song(paths)
            e.acceptProposedAction()
            return
        super().dropEvent(e)
