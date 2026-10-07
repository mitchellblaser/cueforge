"""AI ▸ Compare suggestions with my cues: how much of a finished song's programming the
analysis would have suggested, per type and per lane, with thresholds learnt from it."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core.compare import compare
from ..core.model import KIND_LABELS
from . import theme


class CompareDialog(QDialog):
    def __init__(self, session, parent=None, analyse=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare suggestions with my cues")
        self.setMinimumSize(720, 560)
        self.s = session
        self._analyse = analyse
        lay = QVBoxLayout(self)
        intro = QLabel("For songs you've already programmed: how many of your cues the analysis would have "
                       "suggested, and how many of its suggestions land on one of your cues (within a quarter "
                       "beat). Uses each song's last analysis in this session.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.FG_DIM};")
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem("This song", "this")
        self.scope.addItem("All analysed songs with cues", "all")
        self.scope.currentIndexChanged.connect(self.refresh)
        row.addWidget(QLabel("Compare:"))
        row.addWidget(self.scope, 1)
        self.analyse_btn = QPushButton("Analyse songs first…")
        self.analyse_btn.clicked.connect(self._run_analysis)
        row.addWidget(self.analyse_btn)
        lay.addLayout(row)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.kinds = QTableWidget(0, 6)
        self.kinds.setHorizontalHeaderLabels(["Type", "Shown", "On your cues", "Your cues found",
                                              "Threshold", "From your cues"])
        self.kinds.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.kinds.verticalHeader().hide()
        lay.addWidget(self.kinds, 1)
        self.lanes = QTableWidget(0, 3)
        self.lanes.setHorizontalHeaderLabels(["Your lane", "Cues", "Suggested by the analysis"])
        self.lanes.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.lanes.verticalHeader().hide()
        lay.addWidget(self.lanes, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.apply_btn = bb.addButton("Use the thresholds learnt from my cues", QDialogButtonBox.ApplyRole)
        self.apply_btn.clicked.connect(self._apply)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.report = None
        self.refresh()

    def _songs(self):
        p = self.s.project
        if self.scope.currentData() == "this":
            songs = [p.song]
        else:
            songs = [x for x in p.songs if x.cues]
        have = [(x, self.s.raw_analysis[x.id]) for x in songs if x.id in self.s.raw_analysis and x.cues]
        missing = [x for x in songs if x.cues and x.id not in self.s.raw_analysis]
        return have, missing

    def refresh(self) -> None:
        have, missing = self._songs()
        self.analyse_btn.setVisible(bool(missing) and self._analyse is not None)
        self.analyse_btn.setText(f"Analyse {len(missing)} song(s) first…")
        p = self.s.project
        if not have:
            self.summary.setText("<b>Nothing to compare yet.</b> " + (
                "Analyse the song(s) first: the comparison uses the analysis results from this session."
                if missing else "This needs a song that has cues of yours."))
            self.kinds.setRowCount(0)
            self.lanes.setRowCount(0)
            self.apply_btn.setEnabled(False)
            return
        rep = self.report = compare(have, p.lanes, p.analysis.thresholds)
        share = rep.found / rep.cues if rep.cues else 0.0
        self.summary.setText(
            f"<b>{rep.songs} song(s), {rep.cues} of your cues.</b> The shown suggestions land on "
            f"<b>{share:.0%}</b> of them, with {rep.shown} suggestions ({rep.per_minute:.0f} a minute)."
            + (f"<br><span style='color:#ffb74d'>{len(missing)} song(s) with cues weren't analysed in this "
               f"session and are left out.</span>" if missing else ""))
        self.kinds.setRowCount(len(rep.kinds))
        for i, r in enumerate(rep.kinds):
            vals = [KIND_LABELS.get(r.kind, r.kind), str(r.shown),
                    f"{r.on_cues} ({r.precision:.0%})" if r.shown else "–",
                    f"{r.cues_found}" + (f" (up to {r.cues_reachable} at any confidence)"
                                         if r.cues_reachable > r.cues_found else ""),
                    f"{r.threshold:.0%}",
                    f"{r.suggested:.0%}" if r.suggested is not None else "not enough data"]
            for j, v in enumerate(vals):
                self.kinds.setItem(i, j, QTableWidgetItem(v))
        self.lanes.setRowCount(len(rep.lanes))
        for i, l in enumerate(rep.lanes):
            by = ", ".join(f"{KIND_LABELS.get(k, k)} {n}" for k, n in sorted(l.by_kind.items(), key=lambda x: -x[1]))
            vals = [l.lane, str(l.cues), f"{l.found} ({l.found / l.cues:.0%})" + (f": {by}" if by else "")]
            for j, v in enumerate(vals):
                self.lanes.setItem(i, j, QTableWidgetItem(v))
        self.apply_btn.setEnabled(any(r.suggested is not None for r in rep.kinds))

    def _apply(self) -> None:
        if not self.report:
            return
        for r in self.report.kinds:
            if r.suggested is not None:
                self.s.set_filter(r.kind, threshold=r.suggested)
        self.refresh()

    def _run_analysis(self) -> None:
        _, missing = self._songs()
        if self._analyse and missing:
            self.s.analysis_done.connect(self._analysed)
            self._analyse([x.id for x in missing])

    def _analysed(self, _res) -> None:
        if self.s.analysis_running():
            return
        try:
            self.s.analysis_done.disconnect(self._analysed)
        except (RuntimeError, TypeError):
            pass
        self.refresh()
