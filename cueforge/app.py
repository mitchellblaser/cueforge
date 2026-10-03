"""Application entry point."""
from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName("CueForge")
    app.setOrganizationName("CueForge")
    from .ui.theme import apply_theme
    apply_theme(app)
    from .ui.main_window import MainWindow
    win = MainWindow()
    win.restore_layout()
    win.show()
    args = [a for a in argv[1:] if not a.startswith("-")]
    if args:
        if args[0].endswith(".cueproj"):
            win.open_project(os.path.abspath(args[0]))
        else:
            win.s.import_audio([os.path.abspath(a) for a in args])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
