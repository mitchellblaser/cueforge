"""Render the CueForge icon (PNG, plus ICO/ICNS when Qt's image plugins allow)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(os.path.dirname(HERE), "cueforge", "resources")


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256
    p.setBrush(QColor("#16181c"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 48 * s, 48 * s)
    # waveform bars
    import math
    p.setBrush(QColor("#90a4ae"))
    for i in range(16):
        x = 36 * s + i * 12 * s
        h = (20 + 70 * abs(math.sin(i * 0.9)) * (0.4 + 0.6 * (i % 3 == 0))) * s
        p.drawRoundedRect(QRectF(x, 150 * s - h / 2, 7 * s, h), 3 * s, 3 * s)
    # cue markers
    for x, col in ((70, "#4FC3F7"), (130, "#FFB74D"), (190, "#E57373")):
        p.setPen(QPen(QColor(col), 8 * s))
        p.drawLine(QPointF(x * s, 50 * s), QPointF(x * s, 210 * s))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(col))
        p.drawRect(QRectF(x * s, 46 * s, 34 * s, 22 * s))
    p.end()
    return img


def main() -> None:
    app = QGuiApplication(sys.argv)
    os.makedirs(RES, exist_ok=True)
    big = render(512)
    big.save(os.path.join(RES, "icon.png"))
    render(256).save(os.path.join(HERE, "icon.ico"))
    if not render(512).save(os.path.join(HERE, "icon.icns")):
        print("ICNS not supported by this Qt build; macOS build will use the default icon")
    print("icons written")


if __name__ == "__main__":
    main()
