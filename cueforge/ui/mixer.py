"""Compact mixer: one narrow strip per track, plus click, cue blips and master."""
from __future__ import annotations

import math

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout,
                               QWidget)

from ..core.model import TRACK_ROLES
from . import theme
from .session import Session

STRIP_W = 74


def fmt_db(db: float) -> str:
    return "-inf" if db <= -60 else f"{db:+.1f}"


class Fader(QSlider):
    """Vertical dB fader, -60 (off) .. +12 dB. Double-click resets to 0 dB."""
    db_changed = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Vertical, parent)
        self.setRange(-600, 120)
        self.setValue(0)
        self.setFixedHeight(96)
        self.valueChanged.connect(lambda v: self.db_changed.emit(v / 10))

    def set_db(self, db: float) -> None:
        self.blockSignals(True)
        self.setValue(int(round(max(-60, min(12, db)) * 10)))
        self.blockSignals(False)

    def mouseDoubleClickEvent(self, e) -> None:
        self.setValue(0)


class Meter(QWidget):
    def __init__(self, channels: int = 1, parent=None) -> None:
        super().__init__(parent)
        self.levels = [0.0] * channels
        self.peaks = [0.0] * channels
        self.setFixedSize(6 * channels + 2 * (channels - 1), 96)

    def set_levels(self, values) -> None:
        changed = False
        for i, v in enumerate(values):
            # fast attack, slow release
            new = v if v > self.levels[i] else self.levels[i] * 0.85
            if abs(new - self.levels[i]) > 1e-4:
                changed = True
            self.levels[i] = new
            self.peaks[i] = max(v, self.peaks[i] * 0.97)
        if changed:
            self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        h = self.height()
        for i, lv in enumerate(self.levels):
            x = i * 8
            p.fillRect(QRect(x, 0, 6, h), QColor("#0d0e10"))
            db = 20 * math.log10(lv) if lv > 1e-5 else -60
            frac = max(0.0, min(1.0, (db + 60) / 60))
            bar = int(h * frac)
            col = QColor("#66bb6a") if db < -12 else QColor("#ffca28") if db < -3 else QColor("#ef5350")
            p.fillRect(QRect(x, h - bar, 6, bar), col)
            pdb = 20 * math.log10(self.peaks[i]) if self.peaks[i] > 1e-5 else -60
            py = h - int(h * max(0.0, min(1.0, (pdb + 60) / 60)))
            p.fillRect(QRect(x, py, 6, 1), QColor("#ffffff"))
        p.end()


def _small_button(text: str, checkable: bool = True, color: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setCheckable(checkable)
    b.setFixedSize(26, 20)
    b.setStyleSheet("QPushButton { padding: 0; font-weight: bold; }"
                    + (f"QPushButton:checked {{ background: {color}; color: #111; }}" if color else ""))
    return b


class Strip(QFrame):
    def __init__(self, title: str, color: str = "#888", parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(STRIP_W)
        self.setStyleSheet(f"Strip {{ background: {theme.BG2}; border-top: 3px solid {color}; border-radius: 3px; }}")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(4, 4, 4, 4)
        self.lay.setSpacing(3)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-weight: bold; background: transparent;")
        self.title.setToolTip(title)
        self.lay.addWidget(self.title)
        self.fader = Fader()
        self.meter = Meter()
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addStretch()
        row.addWidget(self.meter)
        row.addWidget(self.fader)
        row.addStretch()
        self.lay.addLayout(row)
        self.db_label = QLabel("0.0")
        self.db_label.setAlignment(Qt.AlignCenter)
        self.db_label.setStyleSheet(f"color: {theme.FG_DIM}; background: transparent; font-size: 10px;")
        self.lay.addWidget(self.db_label)
        self.fader.db_changed.connect(lambda db: self.db_label.setText(fmt_db(db)))

    def set_title(self, text: str) -> None:
        fm = self.title.fontMetrics()
        self.title.setText(fm.elidedText(text, Qt.ElideRight, STRIP_W - 10))
        self.title.setToolTip(text)


class TrackStrip(Strip):
    def __init__(self, session: Session, track_id: str, parent=None) -> None:
        t = session.project.track(track_id)
        super().__init__(t.name, t.color, parent)
        self.s = session
        self.tid = track_id
        self.set_title(t.name)
        self.role = QComboBox()
        self.role.addItems(TRACK_ROLES)
        self.role.setCurrentText(t.role)
        self.role.setStyleSheet("font-size: 10px;")
        self.role.currentTextChanged.connect(self._role)
        self.lay.insertWidget(1, self.role)
        self.fader.set_db(t.gain_db)
        self.db_label.setText(fmt_db(t.gain_db))
        self.fader.db_changed.connect(lambda db: session.update_track(track_id, gain_db=db))
        btns = QHBoxLayout()
        btns.setSpacing(2)
        self.mute = _small_button("M", color="#ef5350")
        self.solo = _small_button("S", color="#ffca28")
        self.mute.setChecked(t.mute)
        self.solo.setChecked(t.solo)
        self.mute.toggled.connect(lambda v: session.update_track(track_id, mute=v))
        self.solo.toggled.connect(lambda v: session.update_track(track_id, solo=v))
        more = QToolButton()
        more.setText("⋯")
        more.setFixedSize(16, 20)
        more.setStyleSheet("padding: 0;")
        more.clicked.connect(lambda: self._menu(more))
        btns.addWidget(self.mute)
        btns.addWidget(self.solo)
        btns.addWidget(more)
        self.lay.addLayout(btns)

    def _role(self, role: str) -> None:
        self.s.update_track(self.tid, role=role, analyse=role in ("Track", "Stem"))

    def refresh(self) -> None:
        t = self.s.project.track(self.tid)
        if not t:
            return
        for w, v in ((self.mute, t.mute), (self.solo, t.solo)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.fader.set_db(t.gain_db)
        self.db_label.setText(fmt_db(t.gain_db))

    def _menu(self, btn) -> None:
        s, t = self.s, self.s.project.track(self.tid)
        m = QMenu(self)
        m.addAction("Rename…", self._rename)
        m.addAction("Set offset…", self._offset)
        a = m.addAction("Use for AI analysis")
        a.setCheckable(True)
        a.setChecked(t.analyse)
        a.setEnabled(t.role in ("Track", "Stem"))
        a.toggled.connect(lambda v: s.update_track(self.tid, analyse=v))
        m.addAction("Move left", lambda: s.move_track(self.tid, -1))
        m.addAction("Move right", lambda: s.move_track(self.tid, 1))
        m.addAction("Relink file…", self._relink)
        m.addSeparator()
        m.addAction("Remove track", lambda: s.remove_track(self.tid))
        m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _rename(self) -> None:
        t = self.s.project.track(self.tid)
        name, ok = QInputDialog.getText(self, "Rename track", "Name:", text=t.name)
        if ok and name.strip():
            self.s.update_track(self.tid, name=name.strip())

    def _offset(self) -> None:
        t = self.s.project.track(self.tid)
        v, ok = QInputDialog.getDouble(self, "Track offset",
                                       "Seconds this file starts after the timeline start\n"
                                       "(negative = trim the start):", t.offset, -3600, 3600, 3)
        if ok:
            self.s.update_track(self.tid, offset=v)

    def _relink(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Relink audio file")
        if path:
            self.s.relink_track(self.tid, path)


class MixerPanel(QWidget):
    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self.strips: dict[str, TrackStrip] = {}
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.inner = QWidget()
        self.row = QHBoxLayout(self.inner)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(4)
        self.scroll.setWidget(self.inner)
        outer.addWidget(self.scroll, 1)

        # fixed strips: click, blips, master
        self.click = Strip("Click", "#ffffff")
        self.click_on = _small_button("On", color="#4FC3F7")
        self.click_on.setFixedWidth(60)
        self.click.lay.insertWidget(1, self.click_on)
        self.click_on.toggled.connect(lambda v: session.update_mixer(click_enabled=v))
        self.click.fader.db_changed.connect(lambda db: session.update_mixer(click_db=db))
        self.click.setToolTip("Click generated from the beat grid (accent on bar 1)")

        self.blips = Strip("Cue blips", "#ffd54f")
        self.blips_on = _small_button("On", color="#4FC3F7")
        self.blips_on.setFixedWidth(60)
        self.blips.lay.insertWidget(1, self.blips_on)
        self.blips_on.toggled.connect(lambda v: session.update_mixer(blips_enabled=v))
        self.blips.fader.db_changed.connect(lambda db: session.update_mixer(blips_db=db))
        self.blips.setToolTip("Short tick on every confirmed cue — hear whether your hits land")

        self.master = Strip("Master", theme.ACCENT)
        self.master.meter.deleteLater()
        self.master.meter = Meter(2)
        self.master.lay.itemAt(1).layout().insertWidget(1, self.master.meter)
        self.master.fader.db_changed.connect(lambda db: session.update_mixer(master_db=db))
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #333;")
        for w in (sep, self.click, self.blips, self.master):
            outer.addWidget(w)

        session.tracks_changed.connect(self.rebuild)
        session.project_replaced.connect(self.rebuild)
        session.mixer_changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._meters)
        self.timer.start()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(196)
        self.rebuild()

    def rebuild(self) -> None:
        while self.row.count():
            item = self.row.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self.strips.clear()
        for t in self.s.project.tracks:
            st = TrackStrip(self.s, t.id)
            self.strips[t.id] = st
            self.row.addWidget(st)
        if not self.s.project.tracks:
            hint = QLabel("Import audio (File ▸ Import audio, or drag files onto the window).\n"
                          "Each file gets a mixer strip with a role: Track, Stem, Click, Cue/Guide or Other.")
            hint.setStyleSheet(f"color: {theme.FG_DIM};")
            self.row.addWidget(hint)
        self.row.addStretch()
        self.refresh()

    def refresh(self) -> None:
        m = self.s.project.mixer
        for st in self.strips.values():
            st.refresh()
        for btn, v in ((self.click_on, m.click_enabled), (self.blips_on, m.blips_enabled)):
            btn.blockSignals(True)
            btn.setChecked(v)
            btn.blockSignals(False)
        for strip, db in ((self.click, m.click_db), (self.blips, m.blips_db), (self.master, m.master_db)):
            strip.fader.set_db(db)
            strip.db_label.setText(fmt_db(db))

    def _meters(self) -> None:
        eng = self.s.engine
        playing = eng.playing
        for tid, st in self.strips.items():
            st.meter.set_levels([eng.meters.get(tid, 0.0) if playing else 0.0])
        self.master.meter.set_levels(eng.master_meter if playing else (0.0, 0.0))
