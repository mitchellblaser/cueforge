"""grandMA3 live link settings and command log."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..control.ma3link import LinkSettings
from . import theme

HELP = """<b>On the console / onPC</b>: Menu ▸ In &amp; Out ▸ OSC ▸ add a line: Destination IP = this
computer, Port = the port below, Mode = UDP, <b>Receive</b> and <b>Receive Command</b> = Yes, Prefix
empty. Then enable the line and <b>Enable Input</b>. CueForge sends command-line text to <code>/cmd</code>
(MA's command address).<br>
<b>Seen in the System Monitor but nothing happens?</b> The console lists every OSC message it
receives, also the ones it ignores (<code>/cmd ,s Go+ …</code> is a correct message: address, "one
string", command). Check <b>Receive Command</b> = Yes, the line and <b>Enable Input</b> are on, and the
address matches the Prefix: empty Prefix → <code>/cmd</code>; Prefix <code>gma3</code> (no slash) →
<code>/gma3/cmd</code>. <i>Send test</i> prints "CueForge link OK" in the console's command line
feedback.<br>
<b>Console edits back to CueForge</b>: add a second OSC line: Destination IP = this computer, Port =
the reply port below, <b>Send</b> = Yes <b>and Send Command</b> = Yes, enabled, and <b>Enable Output</b> on. Put that line's number
(1 = first line) in <i>Console OSC line</i>. CueForge then checks the open song's timecode every 10 s
while stopped and brings edits made on the console back (moved, added, deleted events).<br>
<span style='color:#ffb74d'>Creating cues uses <code>Store … /Merge</code>: keep the programmer
clear while the link creates cues, or your programmer values go into the new cue.</span>"""


TEST_CMD = 'Lua "Printf(\'CueForge link OK\')"'   # harmless: only prints on the console


def short_cmd(cmd: str) -> str:
    """Log text for a command: the timecode push's long Lua chunks shown as one short line."""
    if cmd.startswith('Lua "CF_P=CF_P or {} CF_P['):
        return "Lua … piece " + cmd.split("[", 1)[1].split("]", 1)[0]
    if cmd.startswith('Lua "') and len(cmd) > 160:
        return f"Lua … ({len(cmd)} chars)"
    return cmd


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
        self.reply_port = QSpinBox()
        self.reply_port.setRange(1, 65535)
        self.reply_port.setValue(int(c.reply_port))
        self.reply_port.setToolTip("CueForge listens here for what the console sends back")
        f.addRow("Reply port (this computer)", self.reply_port)
        self.reply_line = QSpinBox()
        self.reply_line.setRange(1, 99)
        self.reply_line.setValue(int(c.reply_line))
        self.reply_line.setToolTip("The number of the console's OSC line that sends to this computer "
                                   "(SendOSC <line>); 1 = the first line in In & Out ▸ OSC")
        f.addRow("Console OSC line (replies)", self.reply_line)
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
        self.tc = QCheckBox("Push timecode shows automatically (sent over the network, no files to copy)")
        self.tc.setChecked(c.push_timecode)
        self.pin = QCheckBox("Fix cue numbers once they exist on the console (new cues get 5.1, 5.2 …)")
        self.pin.setChecked(c.pin_numbers)
        self.pull = QCheckBox("Bring timecode edits made on the console back into CueForge")
        self.pull.setChecked(c.auto_pull)
        for w in (self.preview, self.sync, self.pin, self.delete, self.all, self.tc, self.pull):
            f2.addRow(w)
        self.seq_start = QSpinBox()
        self.seq_start.setRange(1, 99999)
        self.seq_start.setValue(int(link.s.project.export.ma3_seq_start or 1))
        self.seq_start.setToolTip("New sequences are created from this number up (first free numbers), together "
                                  "in one block. They are found by name, so you can move them on the console. "
                                  "Same setting as in the grandMA3 export.")
        f2.addRow("Sequence start", self.seq_start)
        lay.addWidget(g2)

        g3 = QGroupBox("Command syntax ({seq}, {cue}, {label}, {fade})")
        g3.setCheckable(True)
        g3.setChecked(False)
        f3 = QFormLayout(g3)
        self.tpl = {}
        for key, name in (("goto", "Goto"), ("go", "Go+"), ("temp", "Temp"), ("temp_off", "Temp release"),
                          ("store", "Create cue"), ("label", "Label cue"), ("fade", "Cue fade ({fade})"),
                          ("delete", "Delete cue")):
            e = QLineEdit(getattr(c, "cmd_" + key))
            self.tpl[key] = e
            f3.addRow(name, e)
        self.max_cmd = QSpinBox()
        self.max_cmd.setRange(120, 4000)
        self.max_cmd.setValue(int(c.max_cmd or 200))
        self.max_cmd.setToolTip("The timecode push splits its data into commands no longer than this. "
                                "Lower it if the console reports 'unfinished string'.")
        f3.addRow("Longest push command", self.max_cmd)
        reset = QPushButton("Defaults")
        reset.clicked.connect(lambda: [e.setText(getattr(LinkSettings, "cmd_" + k)) for k, e in self.tpl.items()])
        f3.addRow(reset)
        g3.toggled.connect(lambda on: [w.setVisible(on) for w in g3.findChildren(QWidget)])
        for w in g3.findChildren(QWidget):
            w.setVisible(False)
        lay.addWidget(g3)

        btns = QHBoxLayout()
        for txt, fn in (("Send test", self._test), ("Push all cues now", self._push_cues),
                        ("Push timecode now", self._push_tc), ("Pull from console", self._pull),
                        ("Test reply", self._test_reply)):
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
        self.log = QPlainTextEdit("\n".join(short_cmd(c) for c in link.log[-100:]))
        self.log.setReadOnly(True)
        self.log.setFixedHeight(140)
        lay.addWidget(self.log)
        link.sent.connect(self._logged)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _logged(self, cmd: str) -> None:
        self.log.appendPlainText(short_cmd(cmd))

    def _store(self) -> None:
        c = self.link.cfg
        c.enabled = self.enabled.isChecked()
        c.host = self.host.text().strip() or "127.0.0.1"
        c.port = self.port.value()
        c.address = self.addr.text().strip() or "/cmd"
        c.preview = self.preview.isChecked()
        c.sync_cues = self.sync.isChecked()
        c.allow_delete = self.delete.isChecked()
        c.all_songs = self.all.isChecked()
        c.push_timecode = self.tc.isChecked()
        c.auto_pull = self.pull.isChecked()
        c.reply_port = self.reply_port.value()
        c.reply_line = self.reply_line.value()
        self.link.s.project.export.ma3_seq_start = self.seq_start.value()
        c.pin_numbers = self.pin.isChecked()
        for k, e in self.tpl.items():
            setattr(c, "cmd_" + k, e.text().strip() or getattr(LinkSettings, "cmd_" + k))
        c.max_cmd = self.max_cmd.value()
        self.link.save()
        self.link.apply()

    def _test(self) -> None:
        self._store()
        self.link.send(TEST_CMD)

    def _push_cues(self) -> None:
        self._store()
        self.link.push_all()

    def _push_tc(self) -> None:
        self._store()
        self.link.push_timecode()

    def _test_reply(self) -> None:
        self._store()
        self.link.test_reply()

    def _pull(self) -> None:
        self._store()
        self.link.pull_now()

    def accept(self) -> None:
        self._store()
        super().accept()

    def done(self, r) -> None:
        try:
            self.link.sent.disconnect(self._logged)
        except (RuntimeError, TypeError):
            pass
        super().done(r)
