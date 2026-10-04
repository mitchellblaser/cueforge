"""Render the CueForge icon (PNG, plus ICO/ICNS when Qt's image plugins allow)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen, QPolygonF,
                           QRadialGradient)

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(os.path.dirname(HERE), "cueforge", "resources")


def render(size: int) -> QImage:
    """A glowing cue diamond (keyframe) on a faint beat grid: the moment a cue fires."""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256
    bg = QLinearGradient(0, 0, 0, 256 * s)
    bg.setColorAt(0, QColor("#14161c"))
    bg.setColorAt(1, QColor("#0b0c10"))
    p.setBrush(bg)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 52 * s, 52 * s)
    if size >= 64:                                   # the grid is noise at taskbar sizes
        for i, x in enumerate(range(40, 230, 24)):
            p.setPen(QPen(QColor(255, 255, 255, 60 if i % 4 == 0 else 22), (3 if i % 4 == 0 else 2) * s))
            p.drawLine(QPointF(x * s, 40 * s), QPointF(x * s, 216 * s))
    glow = QRadialGradient(QPointF(128 * s, 128 * s), 92 * s)
    glow.setColorAt(0, QColor(255, 140, 40, 200))
    glow.setColorAt(0.45, QColor(255, 90, 30, 70))
    glow.setColorAt(1, QColor(255, 60, 20, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(glow)
    p.drawEllipse(QPointF(128 * s, 128 * s), 92 * s, 92 * s)
    outer = QPolygonF([QPointF(128 * s, 62 * s), QPointF(194 * s, 128 * s), QPointF(128 * s, 194 * s),
                       QPointF(62 * s, 128 * s)])
    fill = QLinearGradient(0, 62 * s, 0, 194 * s)
    fill.setColorAt(0, QColor("#ffe08a"))
    fill.setColorAt(1, QColor("#ff7a1a"))
    p.setBrush(fill)
    p.drawPolygon(outer)
    inner = QPolygonF([QPointF(128 * s, 92 * s), QPointF(164 * s, 128 * s), QPointF(128 * s, 164 * s),
                       QPointF(92 * s, 128 * s)])
    p.setBrush(QColor("#16181c"))
    p.drawPolygon(inner)
    p.end()
    return img


def main() -> None:
    app = QGuiApplication(sys.argv)
    os.makedirs(RES, exist_ok=True)
    big = render(512)
    big.save(os.path.join(RES, "icon.png"))
    render(256).save(os.path.join(HERE, "icon.ico"))
    render(32).save(os.path.join(HERE, "icon_32_preview.png"))
    if not render(512).save(os.path.join(HERE, "icon.icns")):
        print("ICNS not supported by this Qt build; macOS build will use the default icon")
    print("icons written")


if __name__ == "__main__":
    main()
