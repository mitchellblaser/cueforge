"""Import a CuePoints (or any spreadsheet) CSV / TAB cue list."""
from __future__ import annotations

from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout,
                               QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core.timecode import seconds_to_tc
from ..export.cuepoints_import import FIELD_LABELS, FIELDS, ImportOptions, guess_mapping, read_table, rows
from . import theme


class CuePointsImportDialog(QDialog):
    def __init__(self, session, path: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import CuePoints CSV")
        self.resize(820, 600)
        self.s = session
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            self.table_data = read_table(f.read())
        t = self.table_data
        lay = QVBoxLayout(self)
        info = QLabel(f"<b>{len(t.rows)} rows</b> · columns: {', '.join(t.headers) or '—'}<br>"
                      "CuePoints <i>Tracks</i> become songs, <i>Types</i> become lanes. Times are read at the "
                      f"project frame rate ({session.project.frame_rate.label}) — set it first if it differs.")
        info.setWordWrap(True)
        lay.addWidget(info)
        grid = QGridLayout()
        guess = guess_mapping(t)
        self.combos: dict[str, QComboBox] = {}
        for k, f in enumerate(FIELDS):
            cb = QComboBox()
            cb.addItem("—", None)
            for i, h in enumerate(t.headers):
                cb.addItem(h, i)
            cb.setCurrentIndex(max(0, cb.findData(guess.get(f))))
            cb.currentIndexChanged.connect(self._preview)
            self.combos[f] = cb
            grid.addWidget(QLabel(FIELD_LABELS[f]), k // 4 * 2, k % 4)
            grid.addWidget(cb, k // 4 * 2 + 1, k % 4)
        lay.addLayout(grid)
        self.by_song = QCheckBox("Put each Track into the song with that name (new songs are created)")
        self.by_song.setChecked(True)
        self.absolute = QCheckBox("Positions are show timecode (subtract each song's start timecode)")
        self.absolute.setChecked(True)
        self.replace = QCheckBox("Replace the existing cues in the lanes that receive cues")
        for w in (self.by_song, self.absolute, self.replace):
            w.toggled.connect(self._preview)
            lay.addWidget(w)
        self.preview = QTableWidget(0, 6)
        self.preview.setHorizontalHeaderLabels(["Song", "Lane", "Time in song", "Cue", "Label", "Hold"])
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        lay.addWidget(self.preview, 1)
        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet("color: #ffb74d;")
        lay.addWidget(self.problems)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Import")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok = bb.button(QDialogButtonBox.Ok)
        lay.addWidget(bb)
        self._preview()

    def options(self) -> ImportOptions:
        return ImportOptions(mapping={f: cb.currentData() for f, cb in self.combos.items()},
                             by_song=self.by_song.isChecked(), absolute=self.absolute.isChecked(),
                             replace=self.replace.isChecked())

    def items(self):
        return rows(self.table_data, self.options(), self.s.project.frame_rate)

    def _preview(self) -> None:
        p = self.s.project
        opts = self.options()
        items, problems = self.items()
        self.preview.setRowCount(min(len(items), 200))
        for r, it in enumerate(items[:200]):
            song = next((s for s in p.songs if opts.by_song and it.song and s.name.lower() == it.song.lower()), None)
            if opts.by_song and it.song:
                name = it.song + ("" if song else "  (new)")
                start = song.tc_offset if song else (float(int(it.time // 3600) * 3600) if it.time >= 3600 else 0.0)
            else:
                name, start = p.song.name, p.song.tc_offset
            t = it.time - (start if opts.absolute else 0.0)
            vals = [name, it.lane, seconds_to_tc(t, p.frame_rate) if t >= 0 else "before song start",
                    "" if it.number is None else f"{it.number:g}", it.label,
                    "" if not it.duration else f"{it.duration:g}"]
            for c, v in enumerate(vals):
                self.preview.setItem(r, c, QTableWidgetItem(v))
        self.problems.setText(("\n".join(problems[:5]) + (f"\n… {len(problems) - 5} more" if len(problems) > 5 else ""))
                              if problems else "")
        if self.combos["time"].currentData() is None:
            self.problems.setText("Choose the column that holds the cue time (Position).")
        self.ok.setEnabled(bool(items))
        self.ok.setText(f"Import {len(items)} cue(s)")
        self.problems.setStyleSheet(f"color: {'#ffb74d' if problems else theme.FG_DIM};")
