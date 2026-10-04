"""grandMA3 live link settings and command log."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..control.ma3link import LinkSettings, default_timecode_dir
from . import theme

HELP = """<b>On the console / onPC</b>: Menu ▸ In &amp; Out ▸ OSC ▸ add a line: Destination IP = this
computer, Port = the port below, Mode = UDP, <b>Receive</b> and <b>Receive Command</b> ticked, Prefix
<code>gma3</code>. Then enable the OSC line. CueForge sends command-line text to
<code>/gma3/cmd</code>.<br>
<span style='color:#ffb74d'>Creating cues uses <code>Store … /Merge</code>: keep the programmer
clear while the link creates cues, or your programmer values go into the new cue.</span>"""


class LinkDialog(QDialog):
    def __init__(self, link, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("grandMA3 live link")
        self.link = link
        c = link.cfg
        lay = QVBoxLayout(self)
        g = QGroupBox("Connection (OSC)")
        f = QFormLayout(g)
        self.enabled = QCheckBox("Link to grandMA3")
        self.enabled.setChecked(c.enabled)
        f.addRow(self.enabled)
        self.host = QLineEdit(c.host)
        f.addRow("Console / onPC IP", self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(c.port)
        f.addRow("Port", self.port)
        self.addr = QLineEdit(c.address)
        f.addRow("Command address", self.addr)
        lay.addWidget(g)

        g2 = QGroupBox("What to send")
        f2 = QFormLayout(g2)
        self.preview = QCheckBox("Live preview: fire cues on the console while CueForge plays")
        self.preview.setChecked(c.preview)
        self.sync = QCheckBox("Keep cue lists in sync: create and label cues as you program")
        self.sync.setChecked(c.sync_cues)
        self.delete = QCheckBox("Also delete console cues that you delete in CueForge")
        self.delete.setChecked(c.allow_delete)
        self.all = QCheckBox("Sync every song in the setlist (otherwise only the open song)")
        self.all.setChecked(c.all_songs)
        self.tc = QCheckBox("Push timecode shows automatically (onPC on this computer)")
        self.tc.setChecked(c.push_timecode)
        self.pin = QCheckBox("Fix cue numbers once they exist on the console (new cues get 5.1, 5.2 …)")
        self.pin.setChecked(c.pin_numbers)
        for w in (self.preview, self.sync, self.pin, self.delete, self.all, self.tc):
            f2.addRow(w)
        row = QHBoxLayout()
        self.dir = QLineEdit(c.timecode_dir or default_timecode_dir())
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.dir)
        row.addWidget(browse)
        f2.addRow("onPC timecode library", row)
        lay.addWidget(g2)

        g3 = QGroupBox("Command syntax ({seq}, {cue}, {label})")
        g3.setCheckable(True)
        g3.setChecked(False)
        f3 = QFormLayout(g3)
        self.tpl = {}
        for key, name in (("goto", "Goto"), ("go", "Go+"), ("temp", "Temp"), ("temp_off", "Temp release"),
                          ("store", "Create cue"), ("label", "Label cue"), ("delete", "Delete cue"),
                          ("label_seq", "Name sequence")):
            e = QLineEdit(getattr(c, "cmd_" + key))
            self.tpl[key] = e
            f3.addRow(name, e)
        reset = QPushButton("Defaults")
        reset.clicked.connect(lambda: [e.setText(getattr(LinkSettings, "cmd_" + k)) for k, e in self.tpl.items()])
        f3.addRow(reset)
        g3.toggled.connect(lambda on: [w.setVisible(on) for w in g3.findChildren(QWidget)])
        for w in g3.findChildren(QWidget):
            w.setVisible(False)
        lay.addWidget(g3)

        btns = QHBoxLayout()
        for txt, fn in (("Push all cues now", self._push_cues), ("Push timecode now", self._push_tc)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()
        lay.addLayout(btns)
        h = QLabel(HELP)
        h.setWordWrap(True)
        h.setStyleSheet(f"color: {theme.FG_DIM};")
        lay.addWidget(h)
        lay.addWidget(QLabel("Last commands sent:"))
        self.log = QPlainTextEdit("\n".join(link.log[-100:]))
        self.log.setReadOnly(True)
        self.log.setFixedHeight(140)
        lay.addWidget(self.log)
        link.sent.connect(self._logged)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "onPC timecode library folder", self.dir.text())
        if d:
            self.dir.setText(d)

    def _logged(self, cmd: str) -> None:
        self.log.appendPlainText(cmd)

    def _store(self) -> None:
        c = self.link.cfg
        c.enabled = self.enabled.isChecked()
        c.host = self.host.text().strip() or "127.0.0.1"
        c.port = self.port.value()
        c.address = self.addr.text().strip() or "/gma3/cmd"
        c.preview = self.preview.isChecked()
        c.sync_cues = self.sync.isChecked()
        c.allow_delete = self.delete.isChecked()
        c.all_songs = self.all.isChecked()
        c.push_timecode = self.tc.isChecked()
        c.timecode_dir = self.dir.text().strip()
        c.pin_numbers = self.pin.isChecked()
        for k, e in self.tpl.items():
            setattr(c, "cmd_" + k, e.text().strip() or getattr(LinkSettings, "cmd_" + k))
        self.link.save()
        self.link.apply()

    def _push_cues(self) -> None:
        self._store()
        self.link.push_all()

    def _push_tc(self) -> None:
        self._store()
        self.link.push_timecode()

    def accept(self) -> None:
        self._store()
        super().accept()

    def done(self, r) -> None:
        try:
            self.link.sent.disconnect(self._logged)
        except (RuntimeError, TypeError):
            pass
        super().done(r)
