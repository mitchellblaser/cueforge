"""MIDI & OSC control settings, mapping table and MIDI learn."""
from __future__ import annotations

import socket
from dataclasses import asdict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..control.actions import MidiMapping, action_label, all_actions, default_midi_map
from . import theme

OSC_HELP = """<b>OSC addresses</b> (UDP; integer = lane number, float 1/0 = press/release):<br>
<code>/cueforge/lane/3/cue</code> · <code>/cueforge/lane/3/temp</code> — cue / Temp into lane 3<br>
<code>/cueforge/cue</code> · <code>/cueforge/temp</code> — into the active lane (or <code>/cueforge/cue 3</code>)<br>
<code>/cueforge/play</code> · <code>/stop</code> · <code>/loop</code> · <code>/undo</code> · <code>/section</code><br>
<code>/cueforge/lane/next</code> · <code>/lane/prev</code> · <code>/song/next</code> · <code>/song/prev</code><br>
<code>/cueforge/suggestion/next</code> · <code>/prev</code> · <code>/accept</code> · <code>/reject</code><br>
Feedback sent: <code>/cueforge/lane/N/name</code>, <code>/color</code>, <code>/active</code>,
<code>/cueforge/song</code>, <code>/cueforge/tc</code> (while playing)."""


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class ControlDialog(QDialog):
    def __init__(self, hub, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MIDI & OSC control")
        self.hub = hub
        cfg = hub.cfg
        self.maps = cfg.mappings()
        lay = QVBoxLayout(self)

        mg = QGroupBox("MIDI")
        mf = QFormLayout(mg)
        ins, outs = hub.midi_ports()
        self.min = QComboBox()
        self.mout = QComboBox()
        for box, names, cur in ((self.min, ins, cfg.midi_in), (self.mout, outs, cfg.midi_out)):
            box.addItem("(none)", "")
            for n in names:
                box.addItem(n, n)
            if cur and box.findData(cur) < 0:
                box.addItem(f"{cur} (not connected)", cur)
            box.setCurrentIndex(max(0, box.findData(cur)))
        mf.addRow("Input", self.min)
        mf.addRow("Output (pad lights)", self.mout)
        self.min.currentIndexChanged.connect(self._auto_output)
        self.fb = QComboBox()
        self.fb.addItem("Automatic (picks the colour scheme for the connected controller)", "auto")
        self.fb.addItem("Pad colours = lane colours (Launchpad / APC mini mk2 style palette)", "palette")
        self.fb.addItem("Midi Fighter Spectra / 3D colours (channel 3, approximate)", "midifighter")
        self.fb.addItem("On / off (velocity 127)", "onoff")
        self.fb.addItem("No feedback", "off")
        self.fb.setCurrentIndex(max(0, self.fb.findData(cfg.feedback)))
        mf.addRow("Feedback", self.fb)
        self.hold = QCheckBox("Temp pads: hold time = how long the pad is held (otherwise the Hold box)")
        self.hold.setChecked(cfg.hold_from_press)
        mf.addRow(self.hold)
        if not ins and not outs:
            mf.addRow(QLabel(f"<span style='color:{theme.FG_DIM}'>No MIDI ports found. Connect a controller "
                             "and reopen this dialog.</span>"))
        lay.addWidget(mg)

        tg = QGroupBox("MIDI mapping")
        tl = QVBoxLayout(tg)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Control", "Action", "Pad light"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(2, 110)
        tl.addWidget(self.table)
        row = QHBoxLayout()
        self.learn_btn = QPushButton("Learn")
        self.learn_btn.setToolTip("Select a row, click Learn, then press the pad / move the control")
        self.learn_btn.setCheckable(True)
        self.learn_btn.toggled.connect(self._learn)
        for txt, fn in (("Add", self._add), ("Remove", self._remove), ("Reset to defaults", self._reset)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addWidget(self.learn_btn)
        test = QPushButton("Test lights")
        test.setToolTip("Light every pad with a different LED value (0, 8, 16 …) to see which value gives "
                        "which colour on your controller; then type a value into a pad's Pad light")
        test.clicked.connect(self._test_lights)
        row.addWidget(test)
        row.addStretch()
        tl.addLayout(row)
        self.learn_label = QLabel("Default: notes 36–43 = cue into lanes 1–8, notes 44–51 = Temp into lanes 1–8 "
                                  "(any MIDI channel). Pads light in their lane's colour unless you set a Pad light.")
        self.learn_label.setWordWrap(True)
        self.learn_label.setStyleSheet(f"color: {theme.FG_DIM};")
        tl.addWidget(self.learn_label)
        lay.addWidget(tg)

        og = QGroupBox("OSC")
        of = QFormLayout(og)
        self.osc_on = QCheckBox(f"Listen for OSC (this computer: {local_ip()})")
        self.osc_on.setChecked(cfg.osc_enabled)
        of.addRow(self.osc_on)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(cfg.osc_port)
        of.addRow("Listen port", self.port)
        fbrow = QHBoxLayout()
        self.fhost = QLineEdit(cfg.osc_feedback_host)
        self.fport = QSpinBox()
        self.fport.setRange(0, 65535)
        self.fport.setValue(cfg.osc_feedback_port)
        fbrow.addWidget(self.fhost)
        fbrow.addWidget(self.fport)
        of.addRow("Feedback to host / port", fbrow)
        help_ = QLabel(OSC_HELP)
        help_.setTextFormat(Qt.RichText)
        help_.setWordWrap(True)
        help_.setStyleSheet(f"color: {theme.FG_DIM};")
        of.addRow(help_)
        lay.addWidget(og)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        hub.learned.connect(self._learned)
        self._fill()
        self._auto_output()                        # an input is set but no output yet: fill it in

    def _fill(self) -> None:
        n_lanes = max(8, len(self.hub.s.project.lanes))
        actions = all_actions(n_lanes)
        self.table.setRowCount(len(self.maps))
        for r, m in enumerate(self.maps):
            it = QTableWidgetItem(m.describe())
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, it)
            box = QComboBox()
            for a in actions:
                box.addItem(action_label(a), a)
            box.setCurrentIndex(max(0, box.findData(m.action)))
            box.currentIndexChanged.connect(lambda _, r=r, b=box: setattr(self.maps[r], "action", b.currentData()))
            self.table.setCellWidget(r, 1, box)
            led = QSpinBox()
            led.setRange(-1, 127)
            led.setSpecialValueText("lane colour")
            led.setValue(m.led if m.kind == "note" else -1)
            led.setEnabled(m.kind == "note")
            led.setToolTip("LED value sent to this pad. 'lane colour' follows the lane's colour; a number "
                           "(0-127) always lights it that way. Use Test lights to see the values.")
            led.valueChanged.connect(lambda v, r=r: setattr(self.maps[r], "led", int(v)))
            self.table.setCellWidget(r, 2, led)

    def _auto_output(self) -> None:
        """Choosing an input with no output yet picks the same device's output (pad lights)."""
        from ..control.hub import match_port
        name = self.min.currentData() or ""
        if name and not self.mout.currentData():
            outs = [self.mout.itemData(i) for i in range(self.mout.count()) if self.mout.itemData(i)]
            hit = match_port(name, outs)
            if hit:
                self.mout.setCurrentIndex(self.mout.findData(hit))

    def _test_lights(self) -> None:
        cfg = self.hub.cfg
        cfg.midi_in = self.min.currentData() or ""
        cfg.midi_out = self.mout.currentData() or ""
        cfg.feedback = self.fb.currentData()
        cfg.midi_map = [asdict(m) for m in self.maps]
        self.hub.apply()
        shown = self.hub.test_lights()
        if not shown:
            why = getattr(self.hub, "midi_error", "") or ""
            if not why and not cfg.midi_in and not cfg.midi_out:
                why = "Choose your controller as Input (and Output) first."
            elif not why:
                why = "The output port didn't open. Choose your controller as Output (pad lights)."
            self.learn_label.setText(f"<span style='color:#ffb74d'>No pad lights sent.</span> {why}")
            return
        self.learn_label.setText("Pads lit with LED values: " + ", ".join(f"note {n} = {v}" for n, v in shown[:16])
                                 + ". Note the colours you like and type them into Pad light.")

    def _add(self) -> None:
        nxt = max((m.number for m in self.maps if m.kind == "note"), default=35) + 1
        self.maps.append(MidiMapping("note", 0, min(127, nxt), "cue:active"))
        self._fill()
        self.table.selectRow(len(self.maps) - 1)

    def _remove(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.maps):
            del self.maps[r]
            self._fill()

    def _reset(self) -> None:
        self.maps = default_midi_map()
        self._fill()

    def _learn(self, on: bool) -> None:
        if on:
            if self.table.currentRow() < 0:
                self._add()
            self.learn_label.setText("Press a pad or move a control on your MIDI device…")
            self.hub.start_learn()
        else:
            self.hub.stop_learn()

    def _learned(self, m: MidiMapping) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.maps):
            self.maps[r].kind, self.maps[r].channel, self.maps[r].number = m.kind, m.channel, m.number
            self.table.item(r, 0).setText(self.maps[r].describe())
            self.learn_label.setText(f"Learned {m.describe()}")
        self.learn_btn.setChecked(False)

    def accept(self) -> None:
        cfg = self.hub.cfg
        cfg.midi_in = self.min.currentData() or ""
        cfg.midi_out = self.mout.currentData() or ""
        cfg.feedback = self.fb.currentData()
        cfg.hold_from_press = self.hold.isChecked()
        cfg.midi_map = [asdict(m) for m in self.maps]
        cfg.osc_enabled = self.osc_on.isChecked()
        cfg.osc_port = self.port.value()
        cfg.osc_feedback_host = self.fhost.text().strip()
        cfg.osc_feedback_port = self.fport.value()
        self.hub.save()
        self.hub.apply()
        super().accept()

    def done(self, r) -> None:
        self.hub.stop_learn()
        try:
            self.hub.learned.disconnect(self._learned)
        except (RuntimeError, TypeError):
            pass
        super().done(r)
