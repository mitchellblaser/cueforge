"""Dark theme suited to dark show environments.

Note: 8-digit colours are Qt's #AARRGGBB.
"""
from __future__ import annotations

import os

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

# Qt style sheets want forward slashes, also on Windows
RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources").replace("\\", "/")

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
    border-radius: 4px; padding: 3px 6px; min-height: 18px; selection-background-color: #2f5d73; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: #4a5060; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QComboBox:on {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: #5a5f6a;
    background: {BG}; }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 20px;
    border: none; background: transparent; }}
QComboBox::down-arrow {{ image: url({RES}/chevron_down.svg); width: 12px; height: 12px; }}
QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{ image: url({RES}/chevron_down_hover.svg); }}
QComboBox::down-arrow:disabled {{ image: url({RES}/chevron_down_off.svg); }}
QComboBox QAbstractItemView {{ background: {BG2}; border: 1px solid #3a3f4a; border-radius: 4px; padding: 3px;
    outline: none; selection-background-color: #2f5d73; }}
QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 0 6px; border-radius: 3px; }}
QComboBox QAbstractItemView::item:hover {{ background: {BG3}; }}
QComboBox QAbstractItemView::item:selected {{ background: #2f5d73; color: #ffffff; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 18px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 16px; border: none; border-left: 1px solid #2b2f37; border-top-right-radius: 4px;
    background: transparent; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 16px; border: none; border-left: 1px solid #2b2f37; border-bottom-right-radius: 4px;
    background: transparent; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {BG3}; }}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{ background: #2f5d73; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({RES}/chevron_up.svg); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({RES}/chevron_down.svg); width: 10px; height: 10px; }}
QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {{ image: url({RES}/chevron_up_hover.svg); }}
QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {{ image: url({RES}/chevron_down_hover.svg); }}
QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:disabled,
QDoubleSpinBox::up-arrow:off {{ image: url({RES}/chevron_up_off.svg); }}
QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:disabled,
QDoubleSpinBox::down-arrow:off {{ image: url({RES}/chevron_down_off.svg); }}
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
