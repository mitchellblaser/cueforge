"""Side panels: cue list, AI suggestion review, lane setup."""
from __future__ import annotations

import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QSplitter, QTableView, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
                               QVBoxLayout, QWidget, QLineEdit)

from ..core.model import KIND_LABELS, SUGGESTION_KINDS, lane_per_song
from ..core.timecode import parse_tc, seconds_to_tc
from . import theme
from .session import RESERVED_KEYS, Session


# ============================================================== cue list
_DISPLAYROLE, _EDITROLE = int(Qt.DisplayRole.value), int(Qt.EditRole.value)
_BACKGROUNDROLE, _FOREGROUNDROLE = int(Qt.BackgroundRole.value), int(Qt.ForegroundRole.value)
_FONTROLE, _USERROLE, _TOOLTIPROLE = int(Qt.FontRole.value), int(Qt.UserRole.value), int(Qt.ToolTipRole.value)
_ROLES = frozenset((_DISPLAYROLE, _EDITROLE, _BACKGROUNDROLE, _FOREGROUNDROLE, _FONTROLE, _USERROLE, _TOOLTIPROLE))


class CueModel(QAbstractTableModel):
    """The cue list as a lazy model: Qt only asks for the rows on screen, so a rebuild is
    a cheap reset instead of thousands of table items (adding a cue mid-song stays smooth)."""
    COLS = ["Timecode", "Lane", "Cue", "Label", "Fade", "Temp", "Notes", "Src"]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.s = session
        self.cues: list = []
        self.nums: dict[str, float] = {}
        self.lit: dict[str, str] = {}
        self.on_edit = None
        self._rowcache: dict[int, tuple] = {}     # row -> (texts, lane colour, auto number?)
        self._bold = QFont()
        self._bold.setBold(True)
        self._italic = QFont()
        self._italic.setItalic(True)
        self._bold_italic = QFont(self._bold)
        self._bold_italic.setItalic(True)
        self._dim = QColor(theme.FG_DIM)
        self._ai = QColor("#ffd54f")

    def reload(self, cues: list, nums: dict[str, float]) -> None:
        self.beginResetModel()
        self.cues, self.nums = cues, nums
        self.lit = {}
        self._rowcache = {}
        self.endResetModel()

    def _row(self, r: int) -> tuple:
        """Everything a row shows, worked out once (painting asks for every cell and role
        many times a second while the list follows playback)."""
        hit = self._rowcache.get(r)
        if hit is not None:
            return hit
        c = self.cues[r]
        p = self.s.project
        lane = p.lane(c.lane_id)
        num = self.nums.get(c.id)
        texts = (seconds_to_tc(c.time, p.frame_rate, p.tc_offset), lane.name if lane else "?",
                 "" if num is None else f"{num:g}", c.label, "" if c.fade is None else f"{c.fade:g}",
                 "" if not c.duration else f"{c.duration:.2f}", c.notes,
                 "AI" if c.source == "ai-accepted" else "")
        hit = (texts, QColor(lane.color) if lane else None, c.number is None, lane, num)
        self._rowcache[r] = hit
        return hit

    def refresh_rows(self) -> None:
        """Cue values changed in place (no rows added or removed): forget the cached text."""
        self._rowcache = {}
        if self.cues:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.cues) - 1, len(self.COLS) - 1))

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.cues)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLS[section]
        return None

    _EDITABLE = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
    _FIXED = Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def flags(self, index):
        return self._FIXED if index.column() in (1, 7) else self._EDITABLE

    def data(self, index, role=Qt.DisplayRole):
        role = int(role) if not isinstance(role, int) else role
        if role not in _ROLES:                     # plain ints: PySide enum compares are slow here
            return None
        r = index.row()
        if r >= len(self.cues) or r < 0:
            return None
        col = index.column()
        if role == _USERROLE:
            return self.cues[r].id
        texts, lane_col, auto, lane, num = self._row(r)
        if role == _DISPLAYROLE or role == _EDITROLE:
            return texts[col]
        if role == _FOREGROUNDROLE:
            if col == 1:
                return lane_col
            if col == 2 and auto:
                return self._dim
            return self._ai if col == 7 else None
        mode = self.lit.get(self.cues[r].id) if self.lit else None
        if role == _BACKGROUNDROLE:
            if not mode:
                return None
            bg = QColor(lane_col or theme.ACCENT)
            bg.setAlpha(150 if mode == "fired" else 60)
            return QBrush(bg)
        if role == _FONTROLE:
            fired = mode == "fired"
            it = col == 2 and auto
            return (self._bold_italic if it else self._bold) if fired else (self._italic if it else None)
        # tooltip
        p = self.s.project
        from ..core.editing import sequence_name
        seq = f'"{sequence_name(p, lane)}"' if lane else "?"
        if col == 1 and lane:
            return f"grandMA3 Sequence {seq}"
        if col == 2 and num is not None:
            return (f"Automatic: Sequence {seq} Cue {num:g} (in time order from the song's first cue number). "
                    "Type a number to fix it.") if auto else \
                f"Fixed: Sequence {seq} Cue {num:g}. Clear it to number automatically."
        if col == 6 and texts[6]:
            return texts[6]
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid() or self.on_edit is None:
            return False
        self.on_edit(self.cues[index.row()].id, index.column(), str(value))
        return True

    def set_lit(self, state: dict[str, str], rows: dict[str, int]) -> None:
        changed = set(self.lit) | set(state)
        self.lit = state
        for cid in changed:
            r = rows.get(cid)
            if r is not None:
                self.dataChanged.emit(self.index(r, 0), self.index(r, len(self.COLS) - 1),
                                      [Qt.BackgroundRole, Qt.FontRole])


class CueTable(QWidget):
    seek_requested = Signal(float)
    COLS = CueModel.COLS

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self._syncing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.lane_filter = QComboBox()
        self.lane_filter.currentIndexChanged.connect(self.rebuild_now)
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
        self.model = CueModel(session)
        self.model.on_edit = self._edited
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)   # smooth wheel / follow
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setWordWrap(False)
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        self._glide = QPropertyAnimation(self.table.verticalScrollBar(), b"value", self)
        self._glide.setDuration(220)
        self._glide.setEasingCurve(QEasingCurve.OutCubic)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        for i, w in enumerate([92, 80, 44, 120, 40, 40, 80, 28]):
            if i != 3:
                self.table.setColumnWidth(i, w)
        self.table.selectionModel().selectionChanged.connect(self._sel_from_table)
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
        self._tick.setInterval(33)
        self._tick.timeout.connect(self._light)
        self._tick.start()
        self._pending = QTimer(self)                 # several changes in one edit -> one rebuild
        self._pending.setSingleShot(True)
        self._pending.setInterval(0)
        self._pending.timeout.connect(self.rebuild_now)
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
        names = [("All lanes", "")] + [(l.name, l.id) for l in self.s.project.lanes]
        if [(self.lane_filter.itemText(i), self.lane_filter.itemData(i))
                for i in range(self.lane_filter.count())] != names:
            self.lane_filter.blockSignals(True)
            self.lane_filter.clear()
            for n, lid in names:
                self.lane_filter.addItem(n, lid)
            i = self.lane_filter.findData(cur)
            self.lane_filter.setCurrentIndex(max(0, i))
            self.lane_filter.blockSignals(False)
        self.rebuild()

    def rebuild(self) -> None:
        """Ask for a rebuild; several requests in a row are merged into one."""
        self._pending.start()

    def rebuild_now(self) -> None:
        from ..core.editing import effective_cue_numbers
        self._pending.stop()
        p = self.s.project
        lane_id = self.lane_filter.currentData()
        cues = [c for c in p.cues if not lane_id or c.lane_id == lane_id]
        nums: dict[str, float] = {}
        for l in p.lanes:
            nums.update(effective_cue_numbers(p, l.id))
        self._auto_nums = nums
        self._syncing = True
        self.model.reload(cues, nums)
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
        elif not self.isVisible() and not force:
            return                              # hidden tab: no work while playing
        else:
            state, latest = self.active_cues(eng.position())
        self._last_pos = eng.position()
        state = {k: v for k, v in state.items() if k in self._rows}
        if state == self._lit and not force:
            return
        self.model.set_lit(state, self._rows)
        self._lit = state
        if playing and latest in self._rows and latest in state and state[latest] == "fired" \
                and time.monotonic() - self._user_scrolled > 3.0:
            self._glide_to(self._rows[latest])

    def _glide_to(self, row: int) -> None:
        """Scroll so `row` sits in the middle, gliding instead of jumping."""
        sb = self.table.verticalScrollBar()
        h = self.table.rowHeight(row) or 22
        y = self.table.rowViewportPosition(row) + sb.value()
        target = max(sb.minimum(), min(sb.maximum(), int(y + h / 2 - self.table.viewport().height() / 2)))
        if abs(target - sb.value()) < 2:
            return
        if abs(target - sb.value()) > self.table.viewport().height() * 3:
            self._glide.stop()
            sb.setValue(target)                     # far away (a seek): no long slide
            return
        self._glide.stop()
        self._glide.setStartValue(sb.value())
        self._glide.setEndValue(target)
        self._glide.start()

    def _sel_from_session(self) -> None:
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        self._syncing = True
        sel = QItemSelection()
        first = None
        last_col = self.model.columnCount() - 1
        for cid in self.s.sel_cues:
            r = self._rows.get(cid)
            if r is not None:
                sel.select(self.model.index(r, 0), self.model.index(r, last_col))
                first = r if first is None else min(first, r)
        self.table.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)
        if first is not None:
            self.table.scrollTo(self.model.index(first, 0))
        self._syncing = False

    def _sel_from_table(self, *_) -> None:
        if self._syncing:
            return
        ids = {self.model.cues[i.row()].id for i in self.table.selectionModel().selectedRows()
               if i.row() < len(self.model.cues)}
        self._syncing = True
        self.s.select(cues=ids)
        self._syncing = False
        if len(ids) == 1:
            c = self.s.project.cue(next(iter(ids)))
            if c:
                self.seek_requested.emit(c.time)

    def _edited(self, cid: str, col: int, txt: str) -> None:
        txt = txt.strip()
        p = self.s.project
        try:
            if col == 0:
                self.s.update_cue(cid, time=max(0.0, parse_tc(txt, p.frame_rate, p.tc_offset)))
            elif col == 2:
                cue = p.cue(cid)
                auto = getattr(self, "_auto_nums", {}).get(cid)
                if cue is not None and cue.number is None and txt and auto is not None and float(txt) == auto:
                    return                      # unchanged automatic number: keep it automatic
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
        m.addAction(f"Hold / fade for {len(self.s.sel_cues)} cue(s)…", self._hold_fade)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _hold_fade(self) -> None:
        from .dialogs import HoldFadeDialog
        HoldFadeDialog(self.s, set(self.s.sel_cues), self).exec()

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
        self.advance = QCheckBox("Jump to the next suggestion after Accept (A) / Reject (X)")
        self.advance.setChecked(session.auto_advance)
        self.advance.toggled.connect(session.set_auto_advance)
        session.auto_advance_changed.connect(lambda: (self.advance.blockSignals(True),
                                                      self.advance.setChecked(session.auto_advance),
                                                      self.advance.blockSignals(False)))
        lay.addWidget(self.advance)

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

        self._stale = False
        self._pending = QTimer(self)                 # coalesce: one refresh per burst of edits
        self._pending.setSingleShot(True)
        self._pending.setInterval(0)
        self._pending.timeout.connect(self.refresh_now)
        session.cues_changed.connect(self.refresh)
        session.lanes_changed.connect(self.refresh)
        session.project_replaced.connect(self.refresh)
        session.selection_changed.connect(self._sel_from_session)
        session.busy.connect(lambda m: self.analyse_btn.setEnabled(not m.startswith("Analys")))
        self.refresh_now()

    def refresh(self) -> None:
        """Refresh soon; a hidden panel (another tab in front) refreshes when it is shown."""
        if not self.isVisible():
            self._stale = True
            return
        self._pending.start()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self._stale:
            self._stale = False
            self.refresh_now()

    def refresh_now(self) -> None:
        self._pending.stop()
        self._stale = False
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
    COLS = ["Name", "Colour", "Tap key", "Seq from", "Per song", "Export"]

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
        vh = self.table.verticalHeader()
        vh.setVisible(True)
        vh.setSectionsMovable(True)                 # drag the ≡ handle to reorder lanes
        vh.setDefaultAlignment(Qt.AlignCenter)
        vh.sectionMoved.connect(self._header_moved)
        vh.setToolTip("Drag to reorder lanes")
        for i, w in enumerate([0, 56, 56, 64, 60, 50]):
            if w:
                self.table.setColumnWidth(i, w)
        self.table.cellDoubleClicked.connect(self._dbl)
        self.table.itemChanged.connect(self._renamed)
        lay.addWidget(self.table)
        b = QGridLayout()
        for i, (txt, fn) in enumerate([("Add lane", self._add), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                                       ("Down", lambda: self._move(1))]):
            btn = QPushButton(txt)
            btn.clicked.connect(fn)
            b.addWidget(btn, i // 3, i % 3)
        num = QPushButton("Numbering ▾")
        nm = QMenu(num)
        nm.addAction("Number all cues automatically (clear fixed numbers)", lambda: self._auto_numbers(None))
        nm.addAction("Number this lane's cues automatically", lambda: self._auto_numbers(self._lane_id() or False))
        nm.addAction("Renumber this lane from…", self._renumber)
        nm.addSeparator()
        nm.addAction("Number MA3 sequences 1, 2, 3… in lane order", self._number_sequences)
        num.setMenu(nm)
        b.addWidget(num, 1, 1, 1, 2)
        lay.addLayout(b)
        self.dup = QLabel("")
        self.dup.setWordWrap(True)
        self.dup.setStyleSheet("color: #ffb74d;")
        lay.addWidget(self.dup)
        help_ = QLabel("Drag a lane by its ≡ handle (or its header on the timeline) to reorder; keys 1–9 follow the "
                       "order. Each lane exports to a grandMA3 sequence, found on the console by name: \"<song> <lane>\" "
                       "(Per song) or the lane name (shared). Seq from: where a missing sequence is created "
                       "(the first free number from there). Cue numbers are automatic unless you type one.")
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

    def _header_moved(self, logical: int, old_visual: int, new_visual: int) -> None:
        if self._syncing:
            return
        vh = self.table.verticalHeader()
        ids = [self._lane_id(vh.logicalIndex(v)) for v in range(self.table.rowCount())]
        self._syncing = True
        for v in range(self.table.rowCount()):        # put the view back; the model reorders
            vh.moveSection(vh.visualIndex(v), v)
        self._syncing = False
        self.s.reorder_lanes([i for i in ids if i])

    def _auto_numbers(self, lane_id) -> None:
        if lane_id is False:
            QMessageBox.information(self, "Numbering", "Select a lane first.")
            return
        lanes = [lane_id] if lane_id else None
        n = self.s.auto_number_cues(lanes)
        self.s.status.emit(f"{n} cue(s) now numbered automatically" if n else "All cues are already automatic")

    def _number_sequences(self) -> None:
        if QMessageBox.question(self, "Number sequences", "Give the lanes MA3 sequences 1, 2, 3… in lane order? "
                                "Lanes that are already on the console point at new sequences afterwards.") \
                == QMessageBox.Yes:
            self.s.number_sequences()

    def rebuild(self) -> None:
        self._syncing = True
        cur = self._lane_id()
        lanes = self.s.project.lanes
        self.table.setRowCount(len(lanes))
        self.table.setVerticalHeaderLabels(["≡"] * len(lanes))
        names = [l.name.strip().lower() for l in lanes if l.export]   # sequences are found by name
        dups = sorted({l.name for l in lanes if l.export and names.count(l.name.strip().lower()) > 1})
        self.dup.setText(f"⚠ More than one lane is called {', '.join(dups)}: they would share one grandMA3 "
                         "sequence. Rename one." if dups else "")
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
            own = QCheckBox()
            own.setChecked(lane_per_song(l))
            own.setToolTip("On: every song gets its own sequence for this lane, named \"<song> <lane>\", "
                           "cues from 1.\nOff: one sequence shared by all songs, named after the lane "
                           "(e.g. hits, strobe).")
            own.toggled.connect(lambda v, lid=l.id: self.s.update_lane(lid, per_song=v))
            self.table.setCellWidget(r, 4, own)
            ex = QCheckBox()
            ex.setChecked(l.export)
            ex.toggled.connect(lambda v, lid=l.id: self.s.update_lane(lid, export=v))
            self.table.setCellWidget(r, 5, ex)
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
                          f"grandMA3 Timecode {song.ma3_timecode}"
                          + (f", own sequences +{song.seq_offset}" if song.seq_offset else "")
                          + f", cues in shared lanes from {song.cue_start:g}"
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
