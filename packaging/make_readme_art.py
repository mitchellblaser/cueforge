"""Render the README graphics (banner and the how-it-works strip) into docs/images.

    python packaging/make_readme_art.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPolygonF, QRadialGradient)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "docs", "images")
sys.path.insert(0, HERE)
from make_icon import render as render_icon  # noqa: E402

BG0, BG1 = QColor("#0b0c10"), QColor("#16181e")
ORANGE, AMBER = QColor("#ff7a1a"), QColor("#ffe08a")
FG, DIM = QColor("#e8eaee"), QColor("#8a909c")
LANES = ["#4FC3F7", "#FFB74D", "#E57373", "#81C784", "#BA68C8"]


def font(size: float, bold: bool = False) -> QFont:
    f = QFont("DejaVu Sans")
    f.setPixelSize(int(size))
    f.setBold(bold)
    return f


def canvas(w: int, h: int, scale: int = 2):
    img = QImage(w * scale, h * scale, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.scale(scale, scale)
    return img, p


def rounded_bg(p: QPainter, w: int, h: int, r: float = 28) -> None:
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, BG1)
    g.setColorAt(1, BG0)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), r, r)
    p.setClipPath(path)
    p.fillPath(path, g)


def ramp(x: float) -> float:
    """Fade the banner's timeline in from the left."""
    return max(0.0, min(1.0, (x - 836) / 200))


def banner() -> QImage:
    w, h = 1280, 360
    img, p = canvas(w, h)
    rounded_bg(p, w, h)
    # a faint timeline: beat grid, lanes of cue ticks, a playhead
    for i, x in enumerate(range(836, w, 22)):
        k = ramp(x)
        p.setPen(QPen(QColor(255, 255, 255, int((34 if i % 4 == 0 else 12) * k)), 1.4 if i % 4 == 0 else 1))
        p.drawLine(QPointF(x, 40), QPointF(x, h - 40))
    import random
    rnd = random.Random(7)
    for k, col in enumerate(LANES):
        y = 78 + k * 50
        c = QColor(col)
        c.setAlpha(150)
        x = 858
        while x < w - 30:
            if rnd.random() < 0.55:
                if k == 2 and rnd.random() < 0.5:           # Temps: bars with a hold
                    bar = QColor(col)
                    bar.setAlpha(int(70 * ramp(x)))
                    p.fillRect(QRectF(x, y - 12, 26, 24), bar)
                c.setAlpha(int(150 * ramp(x)))
                p.setPen(QPen(c, 2.4))
                p.drawLine(QPointF(x, y - 14), QPointF(x, y + 14))
            x += 22 * rnd.choice([1, 2, 2, 4])
    ph = 1110
    glow = QLinearGradient(ph - 40, 0, ph + 40, 0)
    glow.setColorAt(0, QColor(255, 82, 82, 0))
    glow.setColorAt(0.5, QColor(255, 82, 82, 60))
    glow.setColorAt(1, QColor(255, 82, 82, 0))
    p.fillRect(QRectF(ph - 40, 30, 80, h - 60), glow)
    p.setPen(QPen(QColor("#ff5252"), 2.5))
    p.drawLine(QPointF(ph, 30), QPointF(ph, h - 30))
    # logo + words
    icon = render_icon(512)
    p.drawImage(QRectF(56, 92, 176, 176), icon)
    p.setPen(FG)
    p.setFont(font(84, True))
    p.drawText(QRectF(256, 92, 700, 110), Qt.AlignLeft | Qt.AlignVCenter, "CueForge")
    p.setPen(DIM)
    p.setFont(font(25))
    p.drawText(QRectF(262, 196, 760, 40), Qt.AlignLeft | Qt.AlignVCenter,
               "Lighting cues, programmed to the music.")
    p.setPen(QColor(ORANGE))
    p.setFont(font(19, True))
    p.drawText(QRectF(262, 236, 760, 34), Qt.AlignLeft | Qt.AlignVCenter,
               "AI suggestions  ·  timecode  ·  live grandMA3 link")
    p.end()
    return img


def card(p: QPainter, r: QRectF, title: str, lines: list[str], accent: QColor, draw_icon) -> None:
    path = QPainterPath()
    path.addRoundedRect(r, 18, 18)
    g = QLinearGradient(r.topLeft(), r.bottomLeft())
    g.setColorAt(0, QColor("#1c1f26"))
    g.setColorAt(1, QColor("#14161b"))
    p.fillPath(path, g)
    p.setPen(QPen(QColor(255, 255, 255, 22), 1.2))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.fillRect(QRectF(r.left() + 18, r.top(), r.width() - 36, 3), accent)
    draw_icon(p, QRectF(r.left() + 22, r.top() + 26, 54, 54), accent)
    p.setPen(FG)
    p.setFont(font(21, True))
    p.drawText(QRectF(r.left() + 22, r.top() + 92, r.width() - 40, 30), Qt.AlignLeft | Qt.AlignVCenter, title)
    p.setPen(DIM)
    p.setFont(font(15))
    for i, line in enumerate(lines):
        p.drawText(QRectF(r.left() + 22, r.top() + 128 + i * 24, r.width() - 36, 22),
                   Qt.AlignLeft | Qt.AlignVCenter, line)


def icon_wave(p, r, c):
    p.setPen(QPen(c, 3, Qt.SolidLine, Qt.RoundCap))
    import math
    n = 13
    for i in range(n):
        x = r.left() + i * r.width() / (n - 1)
        a = (0.25 + 0.75 * abs(math.sin(i * 0.9))) * r.height() / 2
        p.drawLine(QPointF(x, r.center().y() - a), QPointF(x, r.center().y() + a))


def icon_ai(p, r, c):
    p.setPen(Qt.NoPen)
    for cx, cy, s in ((0.42, 0.45, 0.42), (0.80, 0.18, 0.2), (0.82, 0.78, 0.16)):
        x, y, s = r.left() + cx * r.width(), r.top() + cy * r.height(), s * r.width()
        star = QPolygonF([QPointF(x, y - s), QPointF(x + s * 0.28, y - s * 0.28), QPointF(x + s, y),
                          QPointF(x + s * 0.28, y + s * 0.28), QPointF(x, y + s), QPointF(x - s * 0.28, y + s * 0.28),
                          QPointF(x - s, y), QPointF(x - s * 0.28, y - s * 0.28)])
        p.setBrush(c)
        p.drawPolygon(star)


def icon_cues(p, r, c):
    for k, col in enumerate(LANES[:3]):
        y = r.top() + 10 + k * 17
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        for j, x in enumerate((0.15, 0.5, 0.82)[: 3 - (k == 2)]):
            q = QColor(col)
            p.setPen(Qt.NoPen)
            p.setBrush(q)
            cx = r.left() + (x + 0.06 * k) * r.width()
            p.drawPolygon(QPolygonF([QPointF(cx, y - 6), QPointF(cx + 6, y), QPointF(cx, y + 6),
                                     QPointF(cx - 6, y)]))


def icon_console(p, r, c):
    body = QRectF(r.left(), r.top() + 8, r.width(), r.height() - 14)
    p.setPen(QPen(c, 2.4))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(body, 6, 6)
    for i in range(5):
        x = body.left() + 9 + i * (body.width() - 18) / 4
        p.setPen(QPen(QColor(255, 255, 255, 60), 2))
        p.drawLine(QPointF(x, body.top() + 10), QPointF(x, body.bottom() - 8))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        y = body.top() + 12 + (i * 7 % 22)
        p.drawRoundedRect(QRectF(x - 4, y, 8, 6), 2, 2)


def arrow(p: QPainter, x0: float, x1: float, y: float, both: bool = False) -> None:
    p.setPen(QPen(QColor(255, 255, 255, 90), 2.2))
    p.drawLine(QPointF(x0, y), QPointF(x1, y))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 120))
    p.drawPolygon(QPolygonF([QPointF(x1 + 2, y), QPointF(x1 - 9, y - 6), QPointF(x1 - 9, y + 6)]))
    if both:
        p.drawPolygon(QPolygonF([QPointF(x0 - 2, y), QPointF(x0 + 9, y - 6), QPointF(x0 + 9, y + 6)]))


def workflow() -> QImage:
    w, h = 1280, 300
    img, p = canvas(w, h)
    rounded_bg(p, w, h, 24)
    cw, gap, top = 272, 44, 28
    x0 = (w - (4 * cw + 3 * gap)) / 2
    cards = [
        ("Audio & stems", ["Mix, stems, click, guide", "Setlist of songs", "Mixer & scrubbing"],
         QColor("#4FC3F7"), icon_wave),
        ("AI analysis", ["Beat grid & bar 1", "Hits, fills, sections,", "chords, lead lines"],
         QColor("#ffb74d"), icon_ai),
        ("Your cues", ["Tap keys & MIDI pads", "Temps, fades, lanes", "Accept or reject ideas"],
         QColor("#81c784"), icon_cues),
        ("grandMA3", ["Plugin or timecode XML", "Live link over OSC", "Console edits sync back"],
         ORANGE, icon_console),
    ]
    for i, (t, lines, acc, ic) in enumerate(cards):
        r = QRectF(x0 + i * (cw + gap), top, cw, h - 2 * top)
        card(p, r, t, lines, acc, ic)
        if i < 3:
            arrow(p, r.right() + 8, r.right() + gap - 8, r.center().y(), both=(i == 2))
    p.end()
    return img


def main() -> None:
    QGuiApplication(sys.argv)
    os.makedirs(OUT, exist_ok=True)
    banner().save(os.path.join(OUT, "banner.png"))
    workflow().save(os.path.join(OUT, "workflow.png"))
    print("written to", OUT)


if __name__ == "__main__":
    main()
