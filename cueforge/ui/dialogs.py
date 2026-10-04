"""Dialogs."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ..analysis.pipeline import AnalysisOptions
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
    """(Beat This!, All-In-One) installed — looked up without importing PyTorch into the UI."""
    import importlib.util
    from .. import addons
    st = addons.status()
    try:
        a1 = importlib.util.find_spec("allin1") is not None
    except (ImportError, ValueError):
        a1 = False
    return st.get("beat_this", False), a1


def _demucs_installed() -> bool:
    from .. import addons
    return addons.status().get("demucs", False)


class AnalysisDialog(QDialog):
    def __init__(self, session, parent=None, scope: str = "this", song_ids: list[str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Analyse audio")
        self.s = session
        lay = QVBoxLayout(self)
        p = session.project
        songs = [x for x in p.songs if session.analysable(x)]
        fresh = [x for x in songs if not x.analysed]
        self._picked = [sid for sid in (song_ids or []) if p.song_by_id(sid)]
        self.scope = QComboBox()
        self.scope.addItem(f"This song ({p.song.name})", "this")
        if len(songs) > 1:
            self.scope.addItem(f"All songs in the setlist ({len(songs)})", "all")
            if fresh and len(fresh) != len(songs):
                self.scope.addItem(f"Songs not analysed yet ({len(fresh)})", "fresh")
        if self._picked:
            self.scope.addItem(f"The {len(self._picked)} song(s) just added", "picked")
            scope = "picked"
        self.scope.setCurrentIndex(max(0, self.scope.findData(scope)))
        srow = QHBoxLayout()
        srow.addWidget(QLabel("<b>Analyse:</b>"))
        srow.addWidget(self.scope, 1)
        lay.addLayout(srow)
        bulk = QLabel("Songs are analysed one after another in the background: keep working, or leave it "
                      "running. Results land in each song as suggestions.")
        bulk.setWordWrap(True)
        bulk.setStyleSheet(f"color: {theme.FG_DIM}; font-size: 10px;")
        lay.addWidget(bulk)
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
        self.demucs.setEnabled(_demucs_installed() and not has_stems)
        self.demucs.setChecked(prev.get("use_demucs", False) and self.demucs.isEnabled())
        if not _demucs_installed():
            self.demucs.setToolTip("Not installed — use Install AI models… below")
        form.addRow(self.demucs)
        if not (bt and _demucs_installed()):
            inst = QPushButton("Install AI models…")
            inst.setToolTip("Beat This! and Demucs: one-time download, installed by CueForge itself")
            inst.clicked.connect(self._install_ai)
            form.addRow(inst)
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

    def _install_ai(self) -> None:
        from .ai_models_dialog import AIModelsDialog
        AIModelsDialog(self.s.settings, self).exec()
        bt, _ = _deep_available()
        self.deep.setToolTip("Installed: " + ("Beat This!" if bt else "none (baseline analysis is used)"))
        has_stems = any(t.role == "Stem" for t in self.s.project.tracks)
        self.demucs.setEnabled(_demucs_installed() and not has_stems)

    def options(self) -> AnalysisOptions:
        o = AnalysisOptions(grid=self.grid.isChecked(), hits=self.hits.isChecked(), fills=self.fills.isChecked(),
                            sections=self.sections.isChecked(), energy=self.energy.isChecked(),
                            harmony=self.harmony.isChecked(), melody=self.melody.isChecked(),
                            melody_from_mix=self.melody_mix.isChecked(),
                            use_deep_models=self.deep.isChecked(), use_demucs=self.demucs.isChecked(),
                            beats_per_bar=self.bpb.value())
        self.s.settings.set("analysis_options", dict(o.__dict__))
        return o

    def song_ids(self) -> list[str]:
        p = self.s.project
        scope = self.scope.currentData()
        if scope == "picked":
            return list(self._picked)
        if scope in ("all", "fresh"):
            return [x.id for x in p.songs if self.s.analysable(x) and (scope == "all" or not x.analysed)]
        return [p.song.id]


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


class PatternDialog(QDialog):
    """Fill a range with cues/Temps every bar / beat / 8th … in one lane."""

    def __init__(self, session, t0: float, t1: float, parent=None) -> None:
        super().__init__(parent)
        from ..core.arrange import PATTERN_STEPS
        self.setWindowTitle("Pattern fill")
        self.s, self.t0, self.t1 = session, t0, t1
        p = session.project
        form = QFormLayout(self)
        form.addRow(QLabel(f"From {seconds_to_tc(t0, p.frame_rate, p.tc_offset)} to "
                           f"{seconds_to_tc(t1, p.frame_rate, p.tc_offset)}"))
        self.lane = QComboBox()
        for l in p.lanes:
            self.lane.addItem(l.name, l.id)
        self.lane.setCurrentIndex(max(0, self.lane.findData(session.active_lane_id)))
        form.addRow("Lane", self.lane)
        self.step = QComboBox()
        for k in PATTERN_STEPS:
            self.step.addItem(k, k)
        self.step.setCurrentIndex(max(0, self.step.findData(session.settings.get("pattern_step", "beat"))))
        form.addRow("Every", self.step)
        self.offset = QDoubleSpinBox()
        self.offset.setRange(0, 15.75)
        self.offset.setSingleStep(0.25)
        self.offset.setSuffix(" beats")
        self.offset.setToolTip("Shift the pattern, e.g. 0.5 for off-beats")
        form.addRow("Offset", self.offset)
        self.kind = QComboBox()
        self.kind.addItem("Cues", False)
        self.kind.addItem(f"Temps (hold {session.temp_hold:g} s)", True)
        form.addRow("Add", self.kind)
        self.label = QLineEdit(session.settings.get("pattern_label", ""))
        self.label.setPlaceholderText("optional, {n} = step number, e.g. Chase {n}")
        form.addRow("Label", self.label)
        self.replace = QCheckBox("Replace existing cues in this lane and range")
        form.addRow(self.replace)
        if not p.beat_grid.beats:
            form.addRow(QLabel("<span style='color:#ffb74d'>No beat grid: steps fall every 0.5 s.</span>"))
        form.addRow(_buttons(self, "Fill"))

    def accept(self) -> None:
        self.s.settings.set("pattern_step", self.step.currentData())
        self.s.settings.set("pattern_label", self.label.text())
        self.s.pattern_fill(self.lane.currentData(), self.t0, self.t1, self.step.currentData(),
                            self.offset.value(), self.kind.currentData(), self.replace.isChecked(),
                            self.label.text().strip())
        super().accept()


class SectionDialog(QDialog):
    def __init__(self, session, sid: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Section")
        self.s, self.sid = session, sid
        m = next(x for x in session.project.sections if x.id == sid)
        form = QFormLayout(self)
        self.name = QLineEdit(m.name)
        self.name.setToolTip("Sections with the same name (ignoring a trailing number) are repeats: "
                             "Chorus 1, Chorus 2 …")
        form.addRow("Name", self.name)
        from ..core import arrange
        n = len(arrange.repeats_of(session.project, m))
        self.all = QCheckBox(f"Rename all {n + 1} '{m.kind}' sections (numbered)")
        self.all.setVisible(n > 0)
        form.addRow(self.all)
        form.addRow(QLabel(f"<span style='color:{theme.FG_DIM}'>Tip: name repeats alike (Verse 1, Verse 2) "
                           "so 'Copy cues to all repeats' finds them.</span>"))
        form.addRow(_buttons(self))
        self.name.selectAll()

    def accept(self) -> None:
        self.s.rename_section(self.sid, self.name.text(), self.all.isChecked())
        super().accept()


class HoldFadeDialog(QDialog):
    """Hold / fade for all selected cues at once: turn Go cues into Temps and back."""

    def __init__(self, session, cue_ids, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self.ids = list(cue_ids)
        cues = [c for c in session.project.cues if c.id in set(self.ids)]
        self.setWindowTitle(f"Hold / fade — {len(cues)} cue(s)")
        lay = QVBoxLayout(self)
        temps = [c for c in cues if c.duration]
        info = QLabel(f"{len(cues)} cue(s) selected: {len(temps)} Temp(s), {len(cues) - len(temps)} Go cue(s).")
        lay.addWidget(info)
        form = QFormLayout()
        self.hold_mode = QComboBox()
        for txt, key in (("Leave as they are", "keep"), ("Make them Temps with this hold", "temp"),
                         ("Make them Go cues (no hold)", "go")):
            self.hold_mode.addItem(txt, key)
        self.hold_mode.setCurrentIndex(1 if len(temps) < len(cues) else 0)
        form.addRow("Hold", self.hold_mode)
        hold_row = QHBoxLayout()
        self.hold = QDoubleSpinBox()
        self.hold.setRange(0.03, 120.0)
        self.hold.setDecimals(3)
        self.hold.setSingleStep(0.1)
        self.hold.setSuffix(" s")
        self.hold.setValue(temps[0].duration if temps else session.temp_hold or 0.5)
        hold_row.addWidget(self.hold)
        g = session.project.beat_grid
        for txt, mult in (("½ beat", 0.5), ("1 beat", 1.0), ("1 bar", float(g.beats_per_bar))):
            b = QPushButton(txt)
            b.setEnabled(len(g.beats) > 1)
            b.clicked.connect(lambda _=False, m=mult: (self.hold.setValue(60.0 / g.bpm() * m) if g.bpm() else None,
                                                       self.hold_mode.setCurrentIndex(1)))
            hold_row.addWidget(b)
        form.addRow("", hold_row)
        self.fade_mode = QComboBox()
        for txt, key in (("Leave as they are", "keep"), ("Set to", "set"), ("Clear (sequence default)", "clear")):
            self.fade_mode.addItem(txt, key)
        form.addRow("Fade", self.fade_mode)
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0.0, 600.0)
        self.fade.setDecimals(2)
        self.fade.setSuffix(" s")
        fades = [c.fade for c in cues if c.fade is not None]
        self.fade.setValue(fades[0] if fades else 0.0)
        self.fade.valueChanged.connect(lambda _v: self.fade_mode.setCurrentIndex(1))
        form.addRow("", self.fade)
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "Apply"))

    def apply(self) -> int:
        hm, fm = self.hold_mode.currentData(), self.fade_mode.currentData()
        hold = False if hm == "keep" else (self.hold.value() if hm == "temp" else None)
        fade = False if fm == "keep" else (self.fade.value() if fm == "set" else None)
        return self.s.set_hold_fade(self.ids, hold, fade)

    def accept(self) -> None:
        self.apply()
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
    FORMATS = [("lua", "Plugin — run once: creates cues + imports every song's timecode (recommended)"),
               ("xml", "Timecode XML files only (import each into the Timecode pool yourself)"),
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
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(session.settings.get("ma3_format", "lua"))))
        form.addRow("Format", self.fmt)
        self.token = QComboBox()
        self.token.addItem("Go+ (steps to the next cue; phasers keep running)", "Go+")
        self.token.addItem("Goto (jumps to the exact cue; safe when scrubbing)", "Goto")
        self.token.setCurrentIndex(max(0, self.token.findData(ex.ma3_cue_token)))
        form.addRow("Normal cues fire", self.token)
        self.first_goto = QCheckBox("First cue of each lane is a Goto (puts the sequence on the right cue "
                                    "whenever the song starts)")
        self.first_goto.setChecked(ex.ma3_first_goto)
        form.addRow(self.first_goto)
        form.addRow(QLabel(f"<span style='color:{theme.FG_DIM}'>Temps always fire Temp On, then Temp Off "
                           "after their hold.</span>"))
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
        self.create = QCheckBox("Plugin: create missing cues (empty, labelled) in the target sequences")
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
        from ..export.ma3 import go_plus_warnings
        self.gp_warn = QLabel()
        self.gp_warn.setWordWrap(True)
        form.addRow(self.gp_warn)
        self.token.currentIndexChanged.connect(self._update_warnings)
        self._update_warnings()
        warn = QLabel("grandMA3's timecode XML layout is not officially documented. Test in grandMA3 onPC "
                      "first. The plugin falls back to building the timecode itself if the import fails.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.FG_DIM};")
        form.addRow(warn)
        form.addRow(_buttons(self, "Export…"))

    def _update_warnings(self) -> None:
        from ..export.ma3 import go_plus_warnings
        ex = self.s.project.export
        old = ex.ma3_cue_token
        ex.ma3_cue_token = self.token.currentData()
        ws = go_plus_warnings(self.s.project)
        ex.ma3_cue_token = old
        self.gp_warn.setText("" if not ws else "<span style='color:#ffb74d'><b>Go+:</b> " + "<br>".join(ws[:4])
                             + (" …" if len(ws) > 4 else "") + "</span>")

    def accept(self) -> None:
        ex = self.s.project.export
        ex.ma3_time_unit = self.unit.currentData()
        ex.ma3_cue_token = self.token.currentData()
        ex.ma3_first_goto = self.first_goto.isChecked()
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
              ("Hold 1 … 9 while playing", "Drop a Temp for as long as the key is held (snaps to the grid); "
                                           "keys hold independently"),
              ("H", "Hold / fade for all selected cues (turn Go cues into Temps and back)"),
              ("Drag a lane header", "Reorder lanes (keys 1–9 follow the order)"),
              ("Double-click lane", "Add cue (Alt = don't snap)"), ("Double-click cue", "Edit cue"),
              ("Drag cue", "Move (snaps to beats when Snap is on; hold Alt to disable); drag to another lane to move it"),
              ("Shift-drag in ruler", "Set loop region"), ("Delete / Backspace", "Delete selected"),
              ("S", "Snap on/off"), ("G", "Snap selected cues to grid"), ("Ctrl+A", "Select all cues"),
              ("Ctrl+Z / Ctrl+Shift+Z", "Undo / redo"), ("Esc", "Clear selection")]),
    ("Arrange", [("M / Shift+M", "Add section marker at playhead / rename section"),
                 ("Ctrl+C / Ctrl+X / Ctrl+V", "Copy / cut / paste cues at the playhead"),
                 ("Ctrl+Shift+V", "Paste into the section at the playhead, aligned to its start"),
                 ("Ctrl+Shift+C", "Copy this section's cues to all its repeats"),
                 ("Ctrl+P", "Pattern fill (loop region, section or selection)"),
                 ("Section band", "Drag an edge to resize, the middle to move; double-click to rename; "
                                  "right-click for more")]),
    ("Grid (live music)", [("D", "Set bar 1 at the playhead (tap it on the 'one' while playing)"),
                           ("Ctrl+Alt+← / →", "Move bar 1 one beat earlier / later"),
                           ("Drag the end of a Temp", "Change its hold time"),
                           ("Grid ▸ Tap-along grid, then T", "Tap every beat while playing; taps snap to the drums"),
                           ("Grid ▸ Halve / Double tempo", "Fix a grid locked to 8th or half notes")]),
    ("AI suggestions", [("Tab / Shift+Tab", "Jump to next / previous suggestion"),
                        ("A", "Accept selected suggestion(s)"), ("X", "Reject selected suggestion(s)"),
                        ("Double-click suggestion", "Accept"),
                        ("Right-click lead line", "Accept as chase steps (one cue per note)")]),
    ("View", [("Ctrl + wheel / + / -", "Zoom"), ("Wheel / Shift+wheel", "Scroll"), ("F", "Follow playhead on/off"),
              ("Z", "Zoom to fit"), ("Ctrl+Alt+S", "Scrub audio on/off")]),
    ("grandMA3", [("Ctrl+L", "Live link: preview cues on the console while playing, keep cue lists in sync")]),
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
