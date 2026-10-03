"""Dark theme suited to dark show environments.

Note: 8-digit colours are Qt's #AARRGGBB.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#16181c"
BG2 = "#1e2127"
BG3 = "#272b33"
FG = "#d7dae0"
FG_DIM = "#8a909c"
ACCENT = "#4FC3F7"
PLAYHEAD = "#ff5252"
LOOP = "#554caf50"
GRID_BEAT = "#18ffffff"
GRID_BAR = "#40ffffff"
GRID_UNCONFIRMED = "#55ffb74d"
SELECT = "#ffffff"
SUGGESTION = "#ffffff"

STYLE = f"""
QWidget {{ background: {BG}; color: {FG}; font-size: 12px; }}
QMainWindow::separator {{ background: {BG3}; width: 3px; height: 3px; }}
QToolBar {{ background: {BG2}; border: none; spacing: 4px; padding: 3px; }}
QToolButton {{ background: {BG3}; border: 1px solid #333842; border-radius: 4px; padding: 4px 8px; }}
QToolButton:checked {{ background: #2f5d73; border-color: {ACCENT}; }}
QToolButton:hover {{ border-color: {ACCENT}; }}
QPushButton {{ background: {BG3}; border: 1px solid #333842; border-radius: 4px; padding: 4px 10px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:checked {{ background: #2f5d73; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #555; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {BG2}; border: 1px solid #333842;
    border-radius: 3px; padding: 2px 4px; selection-background-color: #2f5d73; }}
QTableWidget, QTreeWidget, QListWidget {{ background: {BG2}; alternate-background-color: #20242a;
    gridline-color: #2b2f37; border: none; selection-background-color: #2f5d73; }}
QHeaderView::section {{ background: {BG3}; color: {FG_DIM}; border: none; padding: 3px; }}
QDockWidget::title {{ background: {BG2}; padding: 4px; color: {FG_DIM}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: {BG2}; padding: 5px 12px; color: {FG_DIM}; }}
QTabBar::tab:selected {{ background: {BG3}; color: {FG}; }}
QMenuBar, QMenu {{ background: {BG2}; }}
QMenu::item:selected {{ background: #2f5d73; }}
QStatusBar {{ background: {BG2}; color: {FG_DIM}; }}
QScrollBar:horizontal {{ background: {BG}; height: 12px; }}
QScrollBar:vertical {{ background: {BG}; width: 12px; }}
QScrollBar::handle {{ background: #3a3f4a; border-radius: 5px; min-width: 20px; min-height: 20px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QSlider::groove:vertical {{ background: #111; width: 4px; border-radius: 2px; }}
QSlider::handle:vertical {{ background: #c8ccd4; height: 10px; margin: 0 -7px; border-radius: 2px; }}
QSlider::groove:horizontal {{ background: #111; height: 4px; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #c8ccd4; width: 10px; margin: -6px 0; border-radius: 2px; }}
QProgressBar {{ background: {BG2}; border: 1px solid #333842; border-radius: 3px; text-align: center; }}
QProgressBar::chunk {{ background: #2f5d73; }}
QGroupBox {{ border: 1px solid #2b2f37; border-radius: 4px; margin-top: 10px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; color: {FG_DIM}; }}
QToolTip {{ background: {BG3}; color: {FG}; border: 1px solid #444; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in [(QPalette.Window, BG), (QPalette.WindowText, FG), (QPalette.Base, BG2),
                      (QPalette.AlternateBase, "#20242a"), (QPalette.Text, FG), (QPalette.Button, BG3),
                      (QPalette.ButtonText, FG), (QPalette.Highlight, "#2f5d73"),
                      (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, BG3),
                      (QPalette.ToolTipText, FG)]:
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
