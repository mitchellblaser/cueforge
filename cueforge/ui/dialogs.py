"""Dialogs."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ..analysis.pipeline import AnalysisOptions
from ..analysis.stems import demucs_available
from ..audio.engine import list_output_devices
from ..core.timecode import FRAME_RATES, parse_tc, seconds_to_tc
from . import theme


def _buttons(dlg: QDialog, ok_text: str = "OK") -> QDialogButtonBox:
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.button(QDialogButtonBox.Ok).setText(ok_text)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    return bb


def _deep_available() -> tuple[bool, bool]:
    try:
        import beat_this  # noqa: F401
        bt = True
    except Exception:
        bt = False
    try:
        import allin1  # noqa: F401
        a1 = True
    except Exception:
        a1 = False
    return bt, a1


class AnalysisDialog(QDialog):
    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Analyse audio")
        self.s = session
        lay = QVBoxLayout(self)
        p = session.project
        tracks = [t for t in p.tracks if t.analyse and t.role in ("Track", "Stem")]
        clicks = [t for t in p.tracks if t.role == "Click"]
        src = ", ".join(t.name for t in tracks) or "<none — set a track's role to Track or Stem>"
        info = QLabel(f"<b>Analysing:</b> {src}" + (f"<br><b>Click track:</b> {clicks[0].name} (used for the grid)"
                                                    if clicks else ""))
        info.setWordWrap(True)
        lay.addWidget(info)
        form = QFormLayout()
        prev = session.settings.get("analysis_options", {})
        self.grid = QCheckBox("Beat grid (tempo, downbeats)")
        self.grid.setChecked(prev.get("grid", True))
        if p.beat_grid.confirmed:
            self.grid.setText("Beat grid — you have a confirmed grid; it will be kept")
        self.hits = QCheckBox("Hits (kick, snare, crash)")
        self.hits.setChecked(prev.get("hits", True))
        self.fills = QCheckBox("Drum fills → strobe suggestions")
        self.fills.setChecked(prev.get("fills", True))
        self.sections = QCheckBox("Sections (verse / chorus / drop changes)")
        self.sections.setChecked(prev.get("sections", True))
        self.energy = QCheckBox("Energy (drops, breakdowns, builds, blackouts)")
        self.energy.setChecked(prev.get("energy", True))
        self.harmony = QCheckBox("Chord changes → colour-change suggestions")
        self.harmony.setChecked(prev.get("harmony", True))
        has_melodic_stems = any(t.role == "Stem" and not any(w in t.name.lower() for w in
                                ("drum", "kick", "snare", "perc", "bass")) for t in p.tracks)
        self.melody = QCheckBox("Lead lines (synth / guitar / vocal phrases, chase steps)")
        self.melody.setChecked(prev.get("melody", True))
        self.melody_mix = QCheckBox("…also estimate lead lines from the full mix (slow, rough)")
        self.melody_mix.setChecked(prev.get("melody_from_mix", False))
        self.melody_mix.setToolTip("Without stems the lead line has to be guessed from the whole mix. "
                                   "Import stems (or use Demucs) for accurate lead-line following.")
        for w in (self.grid, self.hits, self.fills, self.sections, self.energy, self.harmony, self.melody,
                  self.melody_mix):
            form.addRow(w)
        if not has_melodic_stems:
            tip = QLabel("Tip: lead lines are followed accurately from stems (vocals, synth, guitar…). "
                         "Import them with role <b>Stem</b>, or tick Demucs below.")
            tip.setWordWrap(True)
            tip.setStyleSheet(f"color: {theme.FG_DIM};")
            form.addRow(tip)
        bt, a1 = _deep_available()
        self.deep = QCheckBox("Use deep-learning models when installed")
        self.deep.setChecked(prev.get("use_deep_models", True))
        avail = [n for n, ok in (("Beat This!", bt), ("All-In-One", a1)) if ok]
        self.deep.setToolTip("Installed: " + (", ".join(avail) if avail else "none (baseline analysis is used)"))
        form.addRow(self.deep)
        self.demucs = QCheckBox("Separate stems with Demucs when no stems are imported (slow)")
        has_stems = any(t.role == "Stem" for t in tracks)
        self.demucs.setEnabled(demucs_available() and not has_stems)
        self.demucs.setChecked(prev.get("use_demucs", False) and self.demucs.isEnabled())
        if not demucs_available():
            self.demucs.setToolTip("Install with: pip install demucs")
        form.addRow(self.demucs)
        self.bpb = QSpinBox()
        self.bpb.setRange(0, 12)
        self.bpb.setSpecialValueText("Auto (3 or 4)")
        self.bpb.setValue(prev.get("beats_per_bar", 0))
        form.addRow("Beats per bar", self.bpb)
        lay.addLayout(form)
        note = QLabel("Results appear as ghosted suggestions. Your existing cues are never changed, and "
                      "suggestions you already accepted or rejected will not come back.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.FG_DIM};")
        lay.addWidget(note)
        lay.addWidget(_buttons(self, "Analyse"))

    def options(self) -> AnalysisOptions:
        o = AnalysisOptions(grid=self.grid.isChecked(), hits=self.hits.isChecked(), fills=self.fills.isChecked(),
                            sections=self.sections.isChecked(), energy=self.energy.isChecked(),
                            harmony=self.harmony.isChecked(), melody=self.melody.isChecked(),
                            melody_from_mix=self.melody_mix.isChecked(),
                            use_deep_models=self.deep.isChecked(), use_demucs=self.demucs.isChecked(),
                            beats_per_bar=self.bpb.value())
        self.s.settings.set("analysis_options", dict(o.__dict__))
        return o


class TempoDialog(QDialog):
    """Manual grid: BPM + first downbeat. Includes tap tempo."""

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set tempo / beat grid")
        self.s = session
        p = session.project
        g = p.beat_grid
        form = QFormLayout(self)
        self.bpm = QDoubleSpinBox()
        self.bpm.setRange(20, 400)
        self.bpm.setDecimals(3)
        self.bpm.setValue(g.bpm() or 120.0)
        tap = QPushButton("Tap")
        tap.setToolTip("Click (or press T while focused) in time with the music")
        tap.clicked.connect(self._tap)
        row = QHBoxLayout()
        row.addWidget(self.bpm)
        row.addWidget(tap)
        form.addRow("Tempo (BPM)", row)
        self.first = QLineEdit(seconds_to_tc(g.downbeats[0] if g.downbeats else 0.0, p.frame_rate, p.tc_offset))
        use_ph = QPushButton("Use playhead")
        use_ph.clicked.connect(lambda: self.first.setText(
            seconds_to_tc(session.engine.position(), p.frame_rate, p.tc_offset)))
        row2 = QHBoxLayout()
        row2.addWidget(self.first)
        row2.addWidget(use_ph)
        form.addRow("A downbeat (bar 1) at", row2)
        self.bpb = QSpinBox()
        self.bpb.setRange(1, 16)
        self.bpb.setValue(g.beats_per_bar or 4)
        form.addRow("Beats per bar", self.bpb)
        self._taps: list[float] = []
        form.addRow(_buttons(self, "Set grid"))

    def _tap(self) -> None:
        now = time.perf_counter()
        if self._taps and now - self._taps[-1] > 2.0:
            self._taps = []
        self._taps.append(now)
        if len(self._taps) >= 3:
            d = [b - a for a, b in zip(self._taps[:-1], self._taps[1:])]
            self.bpm.setValue(60.0 / (sum(d) / len(d)))

    def accept(self) -> None:
        p = self.s.project
        try:
            first = parse_tc(self.first.text(), p.frame_rate, p.tc_offset)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid timecode", str(exc))
            return
        self.s.grid_from_tempo(self.bpm.value(), first, self.bpb.value())
        super().accept()


class ProjectSettingsDialog(QDialog):
    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project settings")
        self.s = session
        p = session.project
        form = QFormLayout(self)
        self.rate = QComboBox()
        for k, r in FRAME_RATES.items():
            self.rate.addItem(r.label, k)
        self.rate.setCurrentIndex(self.rate.findData(p.frame_rate_key))
        form.addRow("Frame rate", self.rate)
        self.offset = QLineEdit(seconds_to_tc(0, p.frame_rate, p.tc_offset))
        self.offset.setToolTip("Timecode at the start of the current song, e.g. 01:00:00:00 "
                               "(each song has its own; see the setlist ⚙)")
        form.addRow(f"'{p.song.name}' starts at timecode", self.offset)
        form.addRow(QLabel("Changing the frame rate keeps cue times; they are re-snapped to whole frames."))
        form.addRow(_buttons(self))

    def accept(self) -> None:
        from ..core.timecode import get_rate
        from ..core import editing
        key = self.rate.currentData()
        try:
            off = parse_tc(self.offset.text(), get_rate(key), 0.0)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid timecode", str(exc))
            return
        p = self.s.project
        with self.s.edit("Project settings"):
            p.frame_rate_key = key
            p.tc_offset = off
            for c in p.cues:
                c.time = editing.snap_time(p, c.time, False)
        self.s.lanes_changed.emit()
        super().accept()


class SongDialog(QDialog):
    """Per-song settings: name, start timecode, MA3 timecode slot and cue numbering."""

    def __init__(self, session, song_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Song settings")
        self.s, self.sid = session, song_id
        p = session.project
        song = p.song_by_id(song_id)
        form = QFormLayout(self)
        self.name = QLineEdit(song.name)
        form.addRow("Name", self.name)
        self.tc = QLineEdit(seconds_to_tc(0, p.frame_rate, song.tc_offset))
        self.tc.setToolTip("Timecode at the very start of this song's audio, e.g. 02:00:00:00")
        form.addRow("Starts at timecode", self.tc)
        self.slot = QSpinBox()
        self.slot.setRange(1, 9999)
        self.slot.setValue(song.ma3_timecode)
        form.addRow("grandMA3 Timecode slot", self.slot)
        self.cue_start = QDoubleSpinBox()
        self.cue_start.setRange(0.001, 99999)
        self.cue_start.setDecimals(3)
        self.cue_start.setValue(song.cue_start)
        self.cue_start.setToolTip("First automatic cue number for this song (explicit cue numbers are kept)")
        form.addRow("First cue number", self.cue_start)
        self.seq_offset = QSpinBox()
        self.seq_offset.setRange(0, 9999)
        self.seq_offset.setValue(song.seq_offset)
        self.seq_offset.setToolTip("Added to every lane's sequence number for this song "
                                   "(use it if each song has its own block of sequences)")
        form.addRow("Sequence offset", self.seq_offset)
        self.notes = QPlainTextEdit(song.notes)
        self.notes.setFixedHeight(60)
        form.addRow("Notes", self.notes)
        seqs = ", ".join(f"{l.name} → Seq {l.ma3_sequence + song.seq_offset}" for l in p.lanes if l.export)
        info = QLabel(f"<span style='color:{theme.FG_DIM}'>{seqs}</span>")
        info.setWordWrap(True)
        form.addRow(info)
        form.addRow(_buttons(self))

    def accept(self) -> None:
        p = self.s.project
        try:
            off = parse_tc(self.tc.text(), p.frame_rate, 0.0)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid timecode", str(exc))
            return
        self.s.update_song(self.sid, name=self.name.text().strip() or "Song", tc_offset=off,
                           ma3_timecode=self.slot.value(), cue_start=self.cue_start.value(),
                           seq_offset=self.seq_offset.value(), notes=self.notes.toPlainText().strip())
        super().accept()


class CueDialog(QDialog):
    def __init__(self, session, cue_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit cue")
        self.s, self.cid = session, cue_id
        p = session.project
        c = p.cue(cue_id)
        form = QFormLayout(self)
        self.label = QLineEdit(c.label)
        self.tc = QLineEdit(seconds_to_tc(c.time, p.frame_rate, p.tc_offset))
        self.lane = QComboBox()
        for l in p.lanes:
            self.lane.addItem(l.name, l.id)
        self.lane.setCurrentIndex(self.lane.findData(c.lane_id))
        self.number = QLineEdit("" if c.number is None else f"{c.number:g}")
        self.number.setPlaceholderText("auto")
        self.fade = QLineEdit("" if c.fade is None else f"{c.fade:g}")
        self.fade.setPlaceholderText("console default")
        self.hold = QLineEdit("" if not c.duration else f"{c.duration:g}")
        self.hold.setPlaceholderText("empty = normal cue")
        self.hold.setToolTip("Set a hold time to make this a Temp: Temp On at the cue, Temp Off after the hold")
        self.notes = QPlainTextEdit(c.notes)
        self.notes.setFixedHeight(70)
        form.addRow("Label", self.label)
        form.addRow("Timecode", self.tc)
        form.addRow("Lane", self.lane)
        form.addRow("MA3 cue number", self.number)
        form.addRow("Fade (s)", self.fade)
        form.addRow("Temp hold (s)", self.hold)
        form.addRow("Notes", self.notes)
        if c.source == "ai-accepted":
            form.addRow(QLabel("<i>Accepted from an AI suggestion</i>"))
        form.addRow(_buttons(self))
        self.label.setFocus()

    def accept(self) -> None:
        p = self.s.project
        try:
            t = parse_tc(self.tc.text(), p.frame_rate, p.tc_offset)
            num = float(self.number.text()) if self.number.text().strip() else None
            fade = float(self.fade.text()) if self.fade.text().strip() else None
            hold = float(self.hold.text()) if self.hold.text().strip() else None
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid value", str(exc))
            return
        self.s.update_cue(self.cid, label=self.label.text().strip(), time=max(0.0, t), lane_id=self.lane.currentData(),
                          number=num, fade=fade, notes=self.notes.toPlainText().strip(), duration=hold)
        super().accept()


class MA3ExportDialog(QDialog):
    FORMATS = [("xml", "Timecode show XML (import into the Timecode pool)"),
               ("lua", "Lua plugin (creates cues + timecode show on the console)"),
               ("cmd", "Command list (create & label cues only)")]

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export to grandMA3")
        self.s = session
        p = session.project
        ex = p.export
        form = QFormLayout(self)
        self.fmt = QComboBox()
        for k, label in self.FORMATS:
            self.fmt.addItem(label, k)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(session.settings.get("ma3_format", "xml"))))
        form.addRow("Format", self.fmt)
        self.scope = QComboBox()
        self.scope.addItem(f"Current song only ('{p.song.name}')", "current")
        self.scope.addItem(f"Whole setlist ({len(p.songs)} songs)", "all")
        self.scope.setCurrentIndex(1 if len(p.songs) > 1 else 0)
        form.addRow("Songs", self.scope)
        self.tc_num = QSpinBox()
        self.tc_num.setRange(1, 9999)
        self.tc_num.setValue(p.song.ma3_timecode)
        self.tc_num.setToolTip("For the current song; each song's slot is set in its song settings")
        form.addRow("Timecode slot (current song)", self.tc_num)
        self.unit = QComboBox()
        self.unit.addItem("MA3 internal ticks (1/16777216 s)", "ticks")
        self.unit.addItem("Seconds", "seconds")
        self.unit.setCurrentIndex(max(0, self.unit.findData(ex.ma3_time_unit)))
        form.addRow("XML time unit", self.unit)
        self.ver = QLineEdit(ex.ma3_data_version)
        self.ver.setToolTip("Set to your console/onPC software version, e.g. 2.1.1.5")
        form.addRow("XML DataVersion", self.ver)
        self.create = QCheckBox("Lua: create missing cues (empty, labelled) in the target sequences")
        self.create.setChecked(True)
        form.addRow(self.create)
        rows = []
        for song in p.songs:
            n = sum(1 for c in song.cues if (p.lane(c.lane_id) and p.lane(c.lane_id).export))
            rows.append(f"{song.name}: {n} cues → Timecode {song.ma3_timecode}, starts "
                        f"{seconds_to_tc(0, p.frame_rate, song.tc_offset)}")
        form.addRow(QLabel("<b>Setlist:</b><br>" + "<br>".join(rows)))
        from ..export.ma3 import cue_number_clashes
        clashes = cue_number_clashes(p)
        if clashes:
            w = QLabel("<span style='color:#ffb74d'><b>Cue number clash:</b> " + "; ".join(clashes[:3])
                       + (" …" if len(clashes) > 3 else "") + "<br>Give songs different first cue numbers or "
                       "sequence offsets (setlist ⚙, or right-click ▸ Auto-number setlist).</span>")
            w.setWordWrap(True)
            form.addRow(w)
        warn = QLabel("grandMA3's XML layout is not officially documented. Test the import in grandMA3 onPC "
                      "first; if your version rejects it, use the Lua plugin.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.FG_DIM};")
        form.addRow(warn)
        form.addRow(_buttons(self, "Export…"))

    def accept(self) -> None:
        ex = self.s.project.export
        ex.ma3_time_unit = self.unit.currentData()
        self.s.update_song(self.s.project.song.id, ma3_timecode=self.tc_num.value())
        self.all_songs = self.scope.currentData() == "all"
        ex.ma3_data_version = self.ver.text().strip() or ex.ma3_data_version
        self.s.settings.set("ma3_format", self.fmt.currentData())
        super().accept()


class LTCDialog(QDialog):
    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Render LTC audio")
        self.s = session
        ex = session.project.export
        form = QFormLayout(self)
        self.sr = QComboBox()
        for v in (44100, 48000):
            self.sr.addItem(f"{v} Hz", v)
        self.sr.setCurrentIndex(max(0, self.sr.findData(ex.ltc_sample_rate)))
        self.level = QDoubleSpinBox()
        self.level.setRange(-40, 0)
        self.level.setValue(ex.ltc_level_db)
        self.level.setSuffix(" dBFS")
        self.preroll = QDoubleSpinBox()
        self.preroll.setRange(0, 60)
        self.preroll.setValue(ex.ltc_preroll)
        self.preroll.setSuffix(" s")
        self.stereo = QCheckBox("Stereo file: song mix on left, LTC on right")
        form.addRow("Sample rate", self.sr)
        form.addRow("Level", self.level)
        form.addRow("Pre-roll", self.preroll)
        form.addRow(self.stereo)
        p = session.project
        form.addRow(QLabel(f"{p.frame_rate.label}, song start = {seconds_to_tc(0, p.frame_rate, p.tc_offset)}"))
        form.addRow(_buttons(self, "Render…"))

    def accept(self) -> None:
        ex = self.s.project.export
        ex.ltc_sample_rate = self.sr.currentData()
        ex.ltc_level_db = self.level.value()
        ex.ltc_preroll = self.preroll.value()
        super().accept()


class AudioDeviceDialog(QDialog):
    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audio output")
        self.s = session
        form = QFormLayout(self)
        self.dev = QComboBox()
        self.dev.addItem("System default", None)
        for idx, name in list_output_devices():
            self.dev.addItem(name, idx)
        self.dev.setCurrentIndex(max(0, self.dev.findData(session.engine.device)))
        form.addRow("Output device", self.dev)
        status = session.engine.backend
        if session.engine.error:
            status += f" — {session.engine.error}"
        form.addRow("Status", QLabel(status))
        form.addRow(_buttons(self))

    def accept(self) -> None:
        d = self.dev.currentData()
        self.s.engine.set_device(d)
        self.s.settings.set("output_device", d)
        super().accept()


SHORTCUTS = [
    ("Transport", [("Space", "Play / pause"), ("Enter / Home", "Go to start"), ("End", "Go to end"),
                   ("← / →", "Nudge selected cues 1 frame (no selection: move playhead 1 beat)"),
                   ("Shift + ← / →", "Nudge selected cues 1 beat"),
                   ("[ / ]", "Playback speed down / up"), ("I / O", "Loop in / out at playhead"),
                   ("L", "Loop on/off"), ("C", "Click on/off"), ("B", "Cue blips on/off")]),
    ("Cues", [("Q", "＋ Cue: drop a cue in the active lane at the playhead (works while playing)"),
              ("W", "＋ Temp: drop a Temp (cue with the Hold time: Temp On, then Temp Off)"),
              ("↑ / ↓ or click a lane header", "Change the active lane"),
              ("Shift+W / Shift+Q", "Make selected cues Temps / normal cues"),
              ("1 … 9 (lane tap keys)", "Drop a cue in that lane at the playhead (works while playing)"),
              ("Double-click lane", "Add cue (Alt = don't snap)"), ("Double-click cue", "Edit cue"),
              ("Drag cue", "Move (snaps to beats when Snap is on; hold Alt to disable); drag to another lane to move it"),
              ("Shift-drag in ruler", "Set loop region"), ("Delete / Backspace", "Delete selected"),
              ("S", "Snap on/off"), ("G", "Snap selected cues to grid"), ("Ctrl+A", "Select all cues"),
              ("Ctrl+Z / Ctrl+Shift+Z", "Undo / redo"), ("Esc", "Clear selection")]),
    ("Grid (live music)", [("Grid ▸ Tap-along grid, then T", "Tap every beat while playing; taps snap to the drums"),
                           ("Grid ▸ Halve / Double tempo", "Fix a grid locked to 8th or half notes")]),
    ("AI suggestions", [("Tab / Shift+Tab", "Jump to next / previous suggestion"),
                        ("A", "Accept selected suggestion(s)"), ("X", "Reject selected suggestion(s)"),
                        ("Double-click suggestion", "Accept"),
                        ("Right-click lead line", "Accept as chase steps (one cue per note)")]),
    ("View", [("Ctrl + wheel / + / -", "Zoom"), ("Wheel / Shift+wheel", "Scroll"), ("F", "Follow playhead on/off"),
              ("Z", "Zoom to fit")]),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        lay = QVBoxLayout(self)
        html = ""
        for section, items in SHORTCUTS:
            html += f"<h3>{section}</h3><table cellspacing=4>"
            for k, d in items:
                html += f"<tr><td><b>{k}</b></td><td style='padding-left:16px'>{d}</td></tr>"
            html += "</table>"
        lbl = QLabel(html)
        lbl.setTextFormat(Qt.RichText)
        lay.addWidget(lbl)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
