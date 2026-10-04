"""Timeline: ruler, waveform rows, beat grid, cue lanes and suggestion ghosts."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF)
from PySide6.QtWidgets import QGridLayout, QMenu, QScrollBar, QToolTip, QWidget

from ..core.model import KIND_LABELS
from ..core.timecode import seconds_to_tc
from . import theme
from .session import Session

HEADER_W = 160
RULER_H = 38
SECTION_H = 22
TOP_H = RULER_H + SECTION_H   # rows start below the ruler and the section band
TRACK_H = 58
LANE_H = 46
HIT_PX = 6

KIND_SHAPES = {"hit": "diamond", "section": "square", "energy": "triangle", "fill": "bolt",
               "harmony": "circle", "melody": "note"}


@dataclass
class Row:
    kind: str        # "track" | "lane"
    id: str
    y: int           # top in content coordinates (excluding ruler)
    h: int


def _nice_step(min_seconds: float) -> float:
    steps = [1 / 30, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
    for s in steps:
        if s >= min_seconds:
            return s
    return steps[-1]


class TimelineCanvas(QWidget):
    edit_cue_requested = Signal(str)
    rename_section_requested = Signal(str)
    pattern_fill_requested = Signal(float, float)
    view_changed = Signal()

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.s = session
        self.t0 = 0.0
        self.pps = 40.0          # pixels per second
        self.v_off = 0
        self.follow = True
        self.scrub_audio = bool(session.settings.get("scrub_audio", True))
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumHeight(TOP_H + LANE_H)
        self._cache: QPixmap | None = None
        self._rows: list[Row] = []
        self._drag: dict | None = None
        self._last_playhead_x = -1
        self.font_small = QFont()
        self.font_small.setPointSizeF(8.5)
        self.font_bold = QFont()
        self.font_bold.setBold(True)

        for sig in (session.tracks_changed, session.mixer_changed, session.lanes_changed, session.cues_changed,
                    session.selection_changed, session.project_replaced, session.active_lane_changed):
            sig.connect(self.invalidate)
        session.project_replaced.connect(self._on_project)

        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    # ------------------------------------------------------------- geometry
    def _on_project(self) -> None:
        v = self.s.project.view
        self.t0 = float(v.get("t0", 0.0))
        self.pps = float(v.get("pps", 40.0))
        self.v_off = 0
        self.invalidate()

    def store_view(self) -> None:
        self.s.project.view.update({"t0": self.t0, "pps": self.pps})

    def x_of(self, t: float) -> float:
        return HEADER_W + (t - self.t0) * self.pps

    def t_of(self, x: float) -> float:
        return self.t0 + (x - HEADER_W) / self.pps

    @property
    def visible_seconds(self) -> float:
        return max(0.1, (self.width() - HEADER_W) / self.pps)

    def layout_rows(self) -> list[Row]:
        rows = []
        y = 0
        for t in self.s.project.tracks:
            rows.append(Row("track", t.id, y, TRACK_H))
            y += TRACK_H
        for l in self.s.project.lanes:
            rows.append(Row("lane", l.id, y, LANE_H))
            y += LANE_H
        self._rows = rows
        return rows

    def content_height(self) -> int:
        return len(self.s.project.tracks) * TRACK_H + len(self.s.project.lanes) * LANE_H

    def row_at(self, y: float) -> Row | None:
        cy = y - TOP_H + self.v_off
        for r in self._rows:
            if r.y <= cy < r.y + r.h:
                return r
        return None

    def row_rect(self, r: Row) -> QRect:
        return QRect(0, TOP_H + r.y - self.v_off, self.width(), r.h)

    def set_view(self, t0: float | None = None, pps: float | None = None) -> None:
        if pps is not None:
            self.pps = float(min(4000.0, max(2.0, pps)))
        if t0 is not None:
            self.t0 = float(max(-1.0, min(t0, self.s.duration - self.visible_seconds * 0.2)))
        self.invalidate()
        self.view_changed.emit()

    def zoom(self, factor: float, around_t: float | None = None) -> None:
        if around_t is None:
            around_t = self.s.engine.position()
            if not (self.t0 <= around_t <= self.t0 + self.visible_seconds):
                around_t = self.t0 + self.visible_seconds / 2
        x = self.x_of(around_t)
        new_pps = min(4000.0, max(2.0, self.pps * factor))
        self.pps = new_pps
        self.set_view(t0=around_t - (x - HEADER_W) / new_pps)

    def zoom_fit(self) -> None:
        d = self.s.duration
        self.pps = max(2.0, (self.width() - HEADER_W - 20) / max(d, 1))
        self.set_view(t0=0.0)

    def ensure_visible(self, t: float) -> None:
        if not (self.t0 + self.visible_seconds * 0.05 <= t <= self.t0 + self.visible_seconds * 0.9):
            self.set_view(t0=t - self.visible_seconds * 0.3)

    def invalidate(self, *_) -> None:
        self._cache = None
        self.update()

    def resizeEvent(self, e) -> None:
        self.invalidate()
        self.view_changed.emit()

    # ------------------------------------------------------------- playback tick
    def _tick(self) -> None:
        eng = self.s.engine
        pos = eng.position()
        if eng.playing and self.follow and not self._drag:
            right = self.t0 + self.visible_seconds * 0.92
            if pos > right or pos < self.t0:
                self.set_view(t0=pos - self.visible_seconds * 0.08)
        x = int(self.x_of(pos))
        if x != self._last_playhead_x:
            self._last_playhead_x = x
            self.update()

    # ------------------------------------------------------------- painting
    def paintEvent(self, e) -> None:
        if self._cache is None or self._cache.size() != self.size() * self.devicePixelRatioF():
            self._render_cache()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        self._paint_overlay(p)
        p.end()

    def _render_cache(self) -> None:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(self.size() * dpr)
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor(theme.BG))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, False)
        self.layout_rows()
        self._vis_by_lane: dict[str, list] = {}
        for sg in self.s.project.visible_suggestions():
            self._vis_by_lane.setdefault(sg.lane_id, []).append(sg)
        w = self.width()
        body = QRect(HEADER_W, TOP_H, w - HEADER_W, self.height() - TOP_H)

        # rows background + content
        p.save()
        p.setClipRect(QRect(0, TOP_H, w, self.height() - TOP_H))
        for i, r in enumerate(self._rows):
            rr = self.row_rect(r)
            if rr.bottom() < TOP_H or rr.top() > self.height():
                continue
            if r.kind == "track":
                p.fillRect(rr, QColor("#191c21"))
            elif r.id == self.s.active_lane_id:
                p.fillRect(rr, QColor("#232a33"))
            else:
                p.fillRect(rr, QColor("#1c1f25" if i % 2 else "#1f2229"))
            p.setPen(QColor("#2a2e36"))
            p.drawLine(0, rr.bottom(), w, rr.bottom())
        self._paint_loop(p, body)
        self._paint_grid(p, body)
        for r in self._rows:
            rr = self.row_rect(r)
            if rr.bottom() < TOP_H or rr.top() > self.height():
                continue
            p.save()
            p.setClipRect(QRect(HEADER_W, max(rr.top(), TOP_H), w - HEADER_W, rr.height()))
            if r.kind == "track":
                self._paint_waveform(p, r, rr)
            else:
                self._paint_lane(p, r, rr)
            p.restore()
            self._paint_header(p, r, rr)
        p.restore()
        self._paint_ruler(p)
        self._paint_sections(p)
        p.fillRect(QRect(0, 0, HEADER_W, RULER_H), QColor(theme.BG2))
        p.setPen(QColor(theme.FG_DIM))
        g = self.s.project.beat_grid
        info = f"{g.bpm():.1f} BPM" if g.beats else "No grid"
        if g.beats and not g.confirmed:
            info += " (unconfirmed)"
        p.drawText(QRect(8, 0, HEADER_W - 10, RULER_H), Qt.AlignVCenter | Qt.AlignLeft,
                   f"{self.s.project.frame_rate.label}\n{info}")
        p.end()
        self._cache = pm

    def _paint_loop(self, p: QPainter, body: QRect) -> None:
        loop = self.s.engine.loop
        if loop:
            x0, x1 = self.x_of(loop[0]), self.x_of(loop[1])
            col = QColor(theme.LOOP) if self.s.engine.loop_enabled else QColor("#10ffffff")
            p.fillRect(QRectF(max(x0, HEADER_W), body.top(), max(0, x1 - max(x0, HEADER_W)), body.height()), col)

    def _paint_grid(self, p: QPainter, body: QRect) -> None:
        g = self.s.project.beat_grid
        if not g.beats:
            return
        t1 = self.t0 + self.visible_seconds
        i0 = max(0, bisect.bisect_left(g.beats, self.t0) - 1)
        i1 = bisect.bisect_right(g.beats, t1) + 1
        beats = g.beats[i0:i1]
        if len(beats) < 2:
            return
        spacing = (beats[1] - beats[0]) * self.pps
        downs = set(round(d, 4) for d in g.downbeats)
        beat_col = QColor(theme.GRID_BEAT)
        bar_col = QColor(theme.GRID_BAR) if g.confirmed else QColor(theme.GRID_UNCONFIRMED)
        for b in beats:
            is_bar = round(b, 4) in downs
            if not is_bar and spacing < 12:
                continue
            if is_bar and spacing * g.beats_per_bar < 8:
                continue
            x = int(self.x_of(b))
            if x < HEADER_W:
                continue
            pen = QPen(bar_col if is_bar else beat_col)
            if not g.confirmed:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawLine(x, body.top(), x, body.bottom())

    def _paint_waveform(self, p: QPainter, r: Row, rr: QRect) -> None:
        t = self.s.project.track(r.id)
        audio = self.s.audio.get(r.id)
        if not t:
            return
        if audio is None:
            p.setPen(QColor(theme.FG_DIM))
            msg = self.s.load_errors.get(r.id, "Loading…")
            p.drawText(rr.adjusted(HEADER_W + 10, 0, 0, 0), Qt.AlignVCenter, msg)
            return
        width = rr.width() - HEADER_W
        t0 = self.t0 - t.offset
        t1 = t0 + self.visible_seconds
        mins, maxs = audio.peaks.envelope(t0, t1, width)
        mid = rr.center().y()
        amp = rr.height() / 2 - 4
        any_solo = any(x.solo for x in self.s.project.tracks)
        audible = not t.mute and (t.solo or not any_solo)
        col = QColor(t.color)
        if not audible:
            col.setAlpha(70)
        gain = 10 ** (t.gain_db / 20)
        h = rr.height()
        top = (h / 2 - np.clip(maxs * gain, -1, 1) * amp)
        bot = (h / 2 - np.clip(mins * gain, -1, 1) * amp) + 1
        xs = np.arange(width) + HEADER_W
        valid = (xs >= self.x_of(t.offset)) & (xs <= self.x_of(t.offset + audio.duration))
        # Rasterise the min/max envelope with numpy: far faster than a QPainterPath
        rows = np.arange(h, dtype=np.float32)[:, None]
        mask = (rows >= np.floor(top)[None, :]) & (rows <= np.ceil(bot)[None, :]) & valid[None, :]
        img = np.zeros((h, width, 4), np.uint8)
        img[mask] = (col.blue(), col.green(), col.red(), col.alpha())  # ARGB32 is BGRA in memory
        qimg = QImage(img.data, width, h, width * 4, QImage.Format_ARGB32)
        p.drawImage(HEADER_W, rr.top(), qimg)
        p.setPen(QColor("#12ffffff"))
        p.drawLine(HEADER_W, mid, rr.right(), mid)

    def _paint_lane(self, p: QPainter, r: Row, rr: QRect) -> None:
        proj = self.s.project
        lane = proj.lane(r.id)
        if not lane:
            return
        col = QColor(lane.color)
        t1 = self.t0 + self.visible_seconds
        drag = self._drag if self._drag and self._drag.get("mode") == "move" else None
        fm = QFontMetrics(self.font_small)
        p.setFont(self.font_small)

        # suggestions (ghosts) first, so real cues draw on top
        label_end = -1e9
        last_shape = -1e9
        for sg in self._vis_by_lane.get(r.id, ()):
            if not (self.t0 - 1 <= sg.time <= t1 + 1):
                continue
            x = self.x_of(sg.time)
            sel = sg.id in self.s.sel_sugs
            ghost = QColor("#ffffff")
            ghost.setAlphaF(0.25 + 0.55 * sg.confidence)
            pen = QPen(QColor("#ffd54f") if sel else ghost, 2 if sel else 1, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(x, rr.top() + 4), QPointF(x, rr.bottom() - 4))
            if sg.duration > 0:
                x2 = self.x_of(sg.time + sg.duration)
                band = QRectF(x, rr.bottom() - 17, max(2.0, x2 - x), 14)
                fill = QColor(ghost)
                fill.setAlphaF(0.08 + 0.12 * sg.confidence)
                p.fillRect(band, fill)
                p.drawRect(band)
                if sg.steps and (x2 - x) / max(1, len(sg.steps)) > 3:
                    p.setPen(QPen(ghost, 1))
                    for st in sg.steps:
                        xs = self.x_of(st)
                        p.drawLine(QPointF(xs, band.top() + 2), QPointF(xs, band.bottom() - 2))
            if sel or x - last_shape > 8:  # skip glyphs when markers are densely packed
                self._draw_shape(p, KIND_SHAPES.get(sg.kind, "diamond"), x, rr.bottom() - 10, 5,
                                 QColor("#ffd54f") if sel else ghost, filled=False)
                last_shape = x
            if sg.label and x + 4 > label_end:
                txt = fm.elidedText(sg.label, Qt.ElideRight, 110)
                p.setPen(QColor(ghost))
                p.drawText(QPointF(x + 8, rr.bottom() - 6), txt)
                label_end = x + 8 + fm.horizontalAdvance(txt) + 4

        # confirmed cues
        cues = proj.cues_in_lane(r.id)
        label_end = -1e9
        for c in cues:
            t = c.time
            if drag and c.id in drag["orig"]:
                t = drag["preview"].get(c.id, t)
                if drag.get("target_lane") and drag["target_lane"] != r.id:
                    continue
            if not (self.t0 - 2 <= t <= t1 + 1):
                continue
            x = self.x_of(t)
            sel = c.id in self.s.sel_cues
            if c.duration:
                bar = QColor(theme.SELECT if sel else lane.color)
                bar.setAlpha(60)
                p.fillRect(QRectF(x, rr.top() + 20, max(2.0, c.duration * self.pps), rr.height() - 24), bar)
                xe = self.x_of(t + c.duration)          # end handle: drag to change the hold
                hc = QColor(theme.SELECT if sel else lane.color)
                hc.setAlpha(200)
                p.fillRect(QRectF(xe - 2, rr.top() + 20, 3, rr.height() - 24), hc)
            p.setPen(QPen(QColor(theme.SELECT) if sel else col, 2))
            p.drawLine(QPointF(x, rr.top() + 2), QPointF(x, rr.bottom() - 2))
            label = c.label or (f"{c.number:g}" if c.number is not None else ("Temp" if c.duration else ""))
            if x < label_end and not sel:
                label = ""
            tw = min(fm.horizontalAdvance(label) + 8, 140) if label else 6
            box = QRectF(x, rr.top() + 3, tw, 15)
            p.fillRect(box, QColor(theme.SELECT) if sel else col)
            if label:
                label_end = x + tw + 2
                p.setPen(QColor("#111"))
                p.drawText(box.adjusted(4, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft,
                           fm.elidedText(label, Qt.ElideRight, int(tw - 6)))
            if c.source == "ai-accepted":
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#ffd54f"))
                p.drawEllipse(QPointF(x + 4, rr.bottom() - 7), 2.5, 2.5)
                p.setBrush(Qt.NoBrush)
        # cues dragged in from another lane
        if drag and drag.get("target_lane") == r.id:
            for cid, t in drag["preview"].items():
                c = proj.cue(cid)
                if c and c.lane_id != r.id:
                    x = self.x_of(t)
                    p.setPen(QPen(QColor(theme.SELECT), 2))
                    p.drawLine(QPointF(x, rr.top() + 2), QPointF(x, rr.bottom() - 2))

    def _draw_shape(self, p: QPainter, shape: str, x: float, y: float, s: float, col: QColor, filled: bool):
        p.setPen(QPen(col, 1.2))
        p.setBrush(QBrush(col) if filled else Qt.NoBrush)
        p.setRenderHint(QPainter.Antialiasing, True)
        if shape == "diamond":
            p.drawPolygon(QPolygonF([QPointF(x, y - s), QPointF(x + s, y), QPointF(x, y + s), QPointF(x - s, y)]))
        elif shape == "square":
            p.drawRect(QRectF(x - s * 0.8, y - s * 0.8, s * 1.6, s * 1.6))
        elif shape == "circle":
            p.drawEllipse(QPointF(x, y), s * 0.85, s * 0.85)
        elif shape == "bolt":
            p.drawPolyline(QPolygonF([QPointF(x + s * 0.4, y - s), QPointF(x - s * 0.5, y + s * 0.1),
                                      QPointF(x + s * 0.5, y - s * 0.1), QPointF(x - s * 0.4, y + s)]))
        elif shape == "note":
            p.drawEllipse(QPointF(x - s * 0.3, y + s * 0.5), s * 0.5, s * 0.4)
            p.drawLine(QPointF(x + s * 0.2, y + s * 0.5), QPointF(x + s * 0.2, y - s))
        else:
            p.drawPolygon(QPolygonF([QPointF(x, y - s), QPointF(x + s, y + s), QPointF(x - s, y + s)]))
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setBrush(Qt.NoBrush)

    def _paint_header(self, p: QPainter, r: Row, rr: QRect) -> None:
        hr = QRect(0, rr.top(), HEADER_W, rr.height())
        p.fillRect(hr, QColor(theme.BG2))
        p.setPen(QColor("#2a2e36"))
        p.drawLine(hr.right(), hr.top(), hr.right(), hr.bottom())
        p.drawLine(0, hr.bottom(), HEADER_W, hr.bottom())
        if r.kind == "track":
            t = self.s.project.track(r.id)
            if not t:
                return
            p.fillRect(QRect(0, hr.top(), 4, hr.height()), QColor(t.color))
            p.setPen(QColor(theme.FG))
            p.setFont(self.font_bold)
            p.drawText(hr.adjusted(10, 4, -4, 0), Qt.AlignTop | Qt.AlignLeft,
                       QFontMetrics(self.font_bold).elidedText(t.name, Qt.ElideRight, HEADER_W - 16))
            p.setFont(self.font_small)
            p.setPen(QColor(theme.FG_DIM))
            bits = [t.role]
            if t.offset:
                bits.append(f"{t.offset:+.3f}s")
            if t.mute:
                bits.append("M")
            if t.solo:
                bits.append("S")
            if t.role in ("Track", "Stem") and t.analyse:
                bits.append("AI")
            p.drawText(hr.adjusted(10, 22, -4, 0), Qt.AlignTop | Qt.AlignLeft, " · ".join(bits))
        else:
            lane = self.s.project.lane(r.id)
            if not lane:
                return
            active = lane.id == self.s.active_lane_id
            if active:
                p.fillRect(hr, QColor("#2a3340"))
                p.setPen(QPen(QColor(lane.color), 1))
                p.drawRect(hr.adjusted(0, 0, -1, -1))
            p.fillRect(QRect(0, hr.top(), 7 if active else 4, hr.height()), QColor(lane.color))
            p.setPen(QColor(theme.FG))
            p.setFont(self.font_bold)
            p.drawText(hr.adjusted(10, 4, -4, 0), Qt.AlignTop | Qt.AlignLeft,
                       QFontMetrics(self.font_bold).elidedText(lane.name, Qt.ElideRight, HEADER_W - 40))
            p.setFont(self.font_small)
            p.setPen(QColor(theme.FG_DIM))
            n = len(self.s.project.cues_in_lane(lane.id))
            pend = sum(1 for s in self.s.project.visible_suggestions() if s.lane_id == lane.id)
            sub = ("▶ " if active else "") + f"Seq {lane.ma3_sequence} · {n} cues"
            if pend:
                sub += f" · {pend} AI"
            p.drawText(hr.adjusted(10, 22, -4, 0), Qt.AlignTop | Qt.AlignLeft, sub)
            if lane.tap_key:
                key_r = QRect(HEADER_W - 26, hr.top() + 5, 18, 16)
                p.setPen(QColor(lane.color))
                p.drawRect(key_r)
                p.drawText(key_r, Qt.AlignCenter, lane.tap_key.upper())
        p.setFont(QFont())

    def _paint_ruler(self, p: QPainter) -> None:
        w = self.width()
        p.fillRect(QRect(0, 0, w, RULER_H), QColor(theme.BG2))
        p.setPen(QColor("#2a2e36"))
        p.drawLine(0, RULER_H - 1, w, RULER_H - 1)
        proj = self.s.project
        rate = proj.frame_rate
        step = _nice_step(90 / self.pps)
        minor = step / 5 if step >= 0.25 else step / 2
        t1 = self.t0 + self.visible_seconds
        p.setFont(self.font_small)
        k = math.floor(self.t0 / minor)
        while k * minor <= t1:
            t = k * minor
            x = int(self.x_of(t))
            if x >= HEADER_W:
                major = abs((t / step) - round(t / step)) < 1e-6
                p.setPen(QColor(theme.FG_DIM if major else "#3a3f4a"))
                p.drawLine(x, RULER_H - (14 if major else 6), x, RULER_H - 1)
                if major:
                    p.setPen(QColor(theme.FG))
                    p.drawText(x + 3, 13, seconds_to_tc(t, rate, proj.tc_offset))
            k += 1
        # bar numbers
        g = proj.beat_grid
        if g.downbeats and len(g.downbeats) > 1:
            bar_px = (g.downbeats[1] - g.downbeats[0]) * self.pps
            every = 1
            while bar_px * every < 28:
                every *= 2
            i0 = max(0, bisect.bisect_left(g.downbeats, self.t0) - 1)
            i1 = bisect.bisect_right(g.downbeats, t1) + 1
            col = QColor("#ffcc80") if not g.confirmed else QColor("#9ccc65")
            k1 = g.bar_one_index()
            for i in range(i0, min(i1, len(g.downbeats))):
                n = g.bar_number(i)
                if (n - 1) % every and n != 1:
                    continue
                x = int(self.x_of(g.downbeats[i]))
                if x < HEADER_W:
                    continue
                if i == k1:                          # bar 1 flag
                    f = p.font()
                    f.setBold(True)
                    p.setFont(f)
                    p.setPen(QColor(theme.ACCENT))
                    p.drawText(x + 3, RULER_H - 5, "1")
                    f.setBold(False)
                    p.setFont(f)
                    continue
                p.setPen(col if n > 0 else QColor(theme.FG_DIM))   # count-in bars: 0, -1 …
                p.drawText(x + 3, RULER_H - 5, str(n))
        loop = self.s.engine.loop
        if loop:
            x0, x1 = self.x_of(loop[0]), self.x_of(loop[1])
            col = QColor("#4caf50") if self.s.engine.loop_enabled else QColor("#666")
            p.fillRect(QRectF(max(HEADER_W, x0), 0, max(0, x1 - max(HEADER_W, x0)), 4), col)

    def section_spans(self) -> list[tuple]:
        ms = sorted(self.s.project.sections, key=lambda m: m.time)
        end = self.s.duration
        return [(m, m.time, ms[i + 1].time if i + 1 < len(ms) else end) for i, m in enumerate(ms)]

    def _paint_sections(self, p: QPainter) -> None:
        w = self.width()
        band = QRect(0, RULER_H, w, SECTION_H)
        p.fillRect(band, QColor("#181b20"))
        p.setFont(self.font_small)
        fm = QFontMetrics(self.font_small)
        for m, a, b in self.section_spans():
            x0, x1 = self.x_of(a), self.x_of(b)
            if x1 < HEADER_W or x0 > w:
                continue
            x0c = max(HEADER_W, x0)
            col = QColor(m.color or "#5C6BC0")
            fill = QColor(col)
            fill.setAlpha(90)
            r = QRectF(x0c, RULER_H + 2, max(1.0, x1 - x0c - 1), SECTION_H - 4)
            p.fillRect(r, fill)
            sel = m.id == self.s.sel_section
            p.setPen(QPen(QColor(theme.SELECT) if sel else col, 2 if sel else 1))
            p.drawRect(r)
            if x0 >= HEADER_W:
                p.setPen(QPen(col, 2))
                p.drawLine(QPointF(x0, RULER_H), QPointF(x0, RULER_H + SECTION_H))
            p.setPen(QColor("#ffffff"))
            p.drawText(r.adjusted(5, 0, -3, 0), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(m.name, Qt.ElideRight, int(max(0, r.width() - 8))))
        hr = QRect(0, RULER_H, HEADER_W, SECTION_H)
        p.fillRect(hr, QColor(theme.BG2))
        p.setPen(QColor(theme.FG_DIM))
        p.drawText(hr.adjusted(10, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, "Sections")
        p.drawText(hr.adjusted(10, 0, -8, 0), Qt.AlignVCenter | Qt.AlignRight, "＋ M")
        p.setPen(QColor("#2a2e36"))
        p.drawLine(0, TOP_H - 1, w, TOP_H - 1)

    def hit_section(self, pos) -> tuple[object, str]:
        """(marker, "edge"|"body") under the mouse in the section band."""
        for m, a, b in self.section_spans():
            if abs(self.x_of(a) - pos.x()) <= 5 and self.x_of(a) >= HEADER_W:
                return m, "edge"
        for m, a, b in self.section_spans():
            if self.x_of(a) <= pos.x() < self.x_of(b):
                return m, "body"
        return None, ""

    def _paint_overlay(self, p: QPainter) -> None:
        x = self._last_playhead_x if self._last_playhead_x >= 0 else int(self.x_of(self.s.engine.position()))
        if x >= HEADER_W:
            p.setPen(QPen(QColor(theme.PLAYHEAD), 1.5))
            p.drawLine(x, 0, x, self.height())
            p.setBrush(QColor(theme.PLAYHEAD))
            p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 8)]))
        d = self._drag
        if d and d.get("mode") == "band":
            r = QRectF(d["start"], d["cur"]).normalized()
            p.setPen(QPen(QColor(theme.ACCENT), 1, Qt.DashLine))
            p.setBrush(QColor(79, 195, 247, 30))
            p.drawRect(r)
        if d and d.get("mode") == "loop":
            pass

    # ------------------------------------------------------------- hit testing
    def hit_cue(self, pos) -> str | None:
        r = self.row_at(pos.y())
        if not r or r.kind != "lane" or pos.x() < HEADER_W:
            return None
        best, bd = None, HIT_PX + 1
        for c in self.s.project.cues_in_lane(r.id):
            d = abs(self.x_of(c.time) - pos.x())
            if d < bd:
                best, bd = c.id, d
        return best

    def hit_temp_end(self, pos) -> str | None:
        """A Temp whose end (hold release) is under the pointer, unless its start is closer."""
        r = self.row_at(pos.y())
        if not r or r.kind != "lane" or pos.x() < HEADER_W:
            return None
        best, bd = None, HIT_PX + 1
        for c in self.s.project.cues_in_lane(r.id):
            if not c.duration:
                continue
            de = abs(self.x_of(c.time + c.duration) - pos.x())
            ds = abs(self.x_of(c.time) - pos.x())
            if de < bd and (de < ds or pos.x() > self.x_of(c.time + c.duration) - 1):
                best, bd = c.id, de
        return best

    def hit_suggestion(self, pos) -> str | None:
        r = self.row_at(pos.y())
        if not r or r.kind != "lane" or pos.x() < HEADER_W:
            return None
        best, bd = None, HIT_PX + 1
        for s in self.s.project.visible_suggestions():
            if s.lane_id != r.id:
                continue
            d = abs(self.x_of(s.time) - pos.x())
            if d < bd:
                best, bd = s.id, d
        if best is None:
            # click inside a suggestion's duration band (fills, phrases)
            rr = self.row_rect(r)
            if pos.y() >= rr.bottom() - 18:
                for s in self.s.project.visible_suggestions():
                    if s.lane_id == r.id and s.duration and \
                            self.x_of(s.time) <= pos.x() <= self.x_of(s.time + s.duration):
                        return s.id
        return best

    # ------------------------------------------------------------- mouse
    def mousePressEvent(self, e) -> None:
        pos = e.position()
        mods = e.modifiers()
        add = bool(mods & (Qt.ControlModifier | Qt.ShiftModifier | Qt.MetaModifier))
        if e.button() == Qt.RightButton:
            self._context_menu(e)
            return
        if e.button() != Qt.LeftButton:
            return
        if pos.y() < RULER_H:
            if pos.x() < HEADER_W:
                return
            t = max(0.0, self.t_of(pos.x()))
            if mods & Qt.ShiftModifier:
                self._drag = {"mode": "loop", "t_start": t}
            else:
                self.s.engine.seek(t)
                self._drag = {"mode": "scrub"}
            self.update()
            return
        if pos.y() < TOP_H:                     # section band
            if pos.x() < HEADER_W:
                self.s.add_section_at()             # header: add a section at the playhead
                return
            m, part = self.hit_section(pos)
            if m is not None:
                self.s.select_section(m.id)
                if part == "edge":
                    self._drag = {"mode": "section", "id": m.id, "t": m.time}
            return
        r = self.row_at(pos.y())
        if pos.x() < HEADER_W:
            if r is not None and r.kind == "lane":
                self.s.set_active_lane(r.id)     # click a lane header to make it the active lane
            return
        if r is None:
            return
        if r.kind == "track":
            self.s.engine.seek(max(0.0, self.t_of(pos.x())))
            self._drag = {"mode": "scrub"}
            self.update()
            return
        eid = self.hit_temp_end(pos)
        if eid:
            if eid not in self.s.sel_cues:
                self.s.select(cues={eid}, add=add)
            p = self.s.project
            temps = [c for c in p.cues if c.id in self.s.sel_cues and c.duration] or [p.cue(eid)]
            self._drag = {"mode": "hold", "anchor": eid, "press_x": pos.x(), "moved": False,
                          "orig": {c.id: c.duration for c in temps}}
            return
        cid = self.hit_cue(pos)
        if cid:
            if add:
                self.s.select(cues={cid}, add=True)
            elif cid not in self.s.sel_cues:
                self.s.select(cues={cid})
            if cid in self.s.sel_cues:
                self._drag = {"mode": "move", "press_x": pos.x(), "anchor": cid, "lane": r.id,
                              "orig": {c: self.s.project.cue(c).time for c in self.s.sel_cues if self.s.project.cue(c)},
                              "preview": {}, "moved": False, "target_lane": None}
            return
        sid = self.hit_suggestion(pos)
        if sid:
            self.s.select(sugs={sid}, add=add)
            return
        if not add:
            self.s.select()
        self.s.set_active_lane(r.id)
        self._drag = {"mode": "band", "start": pos, "cur": pos, "add": add, "moved": False}

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        d = self._drag
        if not d:
            self._hover_tip(e)
            return
        self.s.drag_active = d["mode"] in ("hold", "move", "section")
        if d["mode"] == "scrub":
            t = max(0.0, self.t_of(pos.x()))
            self.s.engine.seek(t)
            if self.scrub_audio:
                self.s.engine.scrub(t)
            self.update()
        elif d["mode"] == "loop":
            t = max(0.0, self.t_of(pos.x()))
            a, b = sorted((d["t_start"], t))
            if b - a > 0.05:
                self.s.engine.loop = (a, b)
                self.s.project.loop = (a, b)
                self.invalidate()
        elif d["mode"] == "move":
            dt = (pos.x() - d["press_x"]) / self.pps
            if abs(pos.x() - d["press_x"]) > 2:
                d["moved"] = True
            anchor_t = d["orig"][d["anchor"]] + dt
            if self.s.snap and not (e.modifiers() & Qt.AltModifier):
                b = self.s.project.beat_grid.nearest_beat(anchor_t)
                if b is not None and abs(b - anchor_t) * self.pps < 12:
                    dt = b - d["orig"][d["anchor"]]
            d["preview"] = {cid: max(0.0, t + dt) for cid, t in d["orig"].items()}
            r = self.row_at(pos.y())
            d["target_lane"] = r.id if r and r.kind == "lane" and r.id != d["lane"] else None
            self.invalidate()
        elif d["mode"] == "hold":
            p = self.s.project
            anchor = p.cue(d["anchor"])
            if anchor is None:
                return
            if abs(pos.x() - d["press_x"]) > 2:
                d["moved"] = True
            end = d["orig"][anchor.id] + anchor.time + (pos.x() - d["press_x"]) / self.pps
            if self.s.snap and not (e.modifiers() & Qt.AltModifier):
                g = p.beat_grid
                if len(g.beats) > 1:              # snap the release to beats / half beats
                    from ..core.arrange import beat_at, time_at
                    b = beat_at(g, end)
                    sb = time_at(g, round(b * 2) / 2)
                    if sb is not None and abs(sb - end) * self.pps < 12:
                        end = sb
            frame = 1.0 / p.frame_rate.fps
            delta = max(frame, end - anchor.time) - d["orig"][anchor.id]
            for cid, dur in d["orig"].items():
                c = p.cue(cid)
                if c:
                    c.duration = round(max(frame, dur + delta), 3)   # live; committed on release
            QToolTip.showText(e.globalPosition().toPoint(), f"Hold {anchor.duration:.2f} s", self)
            self.invalidate()
        elif d["mode"] == "band":
            d["cur"] = pos
            d["moved"] = True
            self.update()
        elif d["mode"] == "section":
            t = max(0.0, self.t_of(pos.x()))
            if self.s.snap and not (e.modifiers() & Qt.AltModifier):
                b = self.s.project.beat_grid.nearest_beat(t)
                if b is not None and abs(b - t) * self.pps < 12:
                    t = b
            m = next((x for x in self.s.project.sections if x.id == d["id"]), None)
            if m:
                d["moved"] = True
                m.time = t                       # live preview; committed (with undo) on release
                self.invalidate()

    def mouseReleaseEvent(self, e) -> None:
        d = self._drag
        self._drag = None
        self.s.drag_active = False
        if not d:
            return
        if d["mode"] == "section":
            m = next((x for x in self.s.project.sections if x.id == d["id"]), None)
            if m and d.get("moved"):
                new_t = m.time
                m.time = d["t"]                  # restore, then move through the undo stack
                self.s.move_section(m.id, new_t)
            return
        if d["mode"] == "hold":
            p = self.s.project
            new = {cid: p.cue(cid).duration for cid in d["orig"] if p.cue(cid)}
            for cid, dur in d["orig"].items():        # restore, then change through undo
                if p.cue(cid):
                    p.cue(cid).duration = dur
            if d["moved"] and new != d["orig"]:
                with self.s.edit("Change hold time"):
                    for cid, dur in new.items():
                        p.cue(cid).duration = dur
            self.invalidate()
            return
        if d["mode"] == "move":
            if d["moved"] and d["preview"]:
                self._commit_move(d)
            self.invalidate()
        elif d["mode"] == "band":
            if d["moved"]:
                r = QRectF(d["start"], d["cur"]).normalized()
                ta, tb = self.t_of(r.left()), self.t_of(r.right())
                cues, sugs = set(), set()
                for row in self._rows:
                    rr = self.row_rect(row)
                    if row.kind != "lane" or not r.intersects(QRectF(rr)):
                        continue
                    cues |= {c.id for c in self.s.project.cues_in_lane(row.id) if ta <= c.time <= tb}
                    sugs |= {s.id for s in self.s.project.visible_suggestions()
                             if s.lane_id == row.id and ta <= s.time <= tb}
                if d["add"]:
                    self.s.select(cues | self.s.sel_cues, sugs | self.s.sel_sugs)
                else:
                    self.s.select(cues, sugs)
            else:
                self.s.engine.seek(max(0.0, self.t_of(d["start"].x())))
            self.update()

    def _commit_move(self, d: dict) -> None:
        s = self.s
        from ..core import editing
        with s.edit("Move cues"):
            for c in s.project.cues:
                if c.id in d["preview"]:
                    c.time = editing.snap_time(s.project, d["preview"][c.id], False)
                    if d.get("target_lane"):
                        c.lane_id = d["target_lane"]

    def mouseDoubleClickEvent(self, e) -> None:
        pos = e.position()
        if e.button() == Qt.LeftButton and RULER_H <= pos.y() < TOP_H and pos.x() >= HEADER_W:
            m, _ = self.hit_section(pos)
            if m is not None and m.time <= self.t_of(pos.x()):
                self.rename_section_requested.emit(m.id)
            else:
                self.s.add_section_at(max(0.0, self.t_of(pos.x())))
            return
        if e.button() != Qt.LeftButton or pos.y() < TOP_H or pos.x() < HEADER_W:
            return
        r = self.row_at(pos.y())
        if not r or r.kind != "lane":
            return
        cid = self.hit_cue(pos)
        if cid:
            self.edit_cue_requested.emit(cid)
            return
        sid = self.hit_suggestion(pos)
        if sid:
            self.s.accept([sid])
            return
        snap = self.s.snap and not (e.modifiers() & Qt.AltModifier)
        self.s.add_cue(r.id, max(0.0, self.t_of(pos.x())), snap)

    def wheelEvent(self, e) -> None:
        dx, dy = e.angleDelta().x(), e.angleDelta().y()
        mods = e.modifiers()
        if mods & Qt.ControlModifier or mods & Qt.MetaModifier:
            self.zoom(1.15 ** (dy / 120), self.t_of(e.position().x()))
            return
        if mods & Qt.ShiftModifier and dx == 0:
            dx, dy = dy, 0
        if dx:
            self.set_view(t0=self.t0 - dx / 120 * self.visible_seconds * 0.1)
        if dy:
            extra = self.content_height() - (self.height() - TOP_H)
            if extra > 0:
                self.v_off = int(max(0, min(extra, self.v_off - dy / 2)))
                self.invalidate()
                self.view_changed.emit()
            else:
                self.set_view(t0=self.t0 - dy / 120 * self.visible_seconds * 0.1)

    def _hover_tip(self, e) -> None:
        pos = e.position()
        eid = self.hit_temp_end(pos)
        if eid:
            c = self.s.project.cue(eid)
            QToolTip.showText(e.globalPosition().toPoint(),
                              f"<b>Temp release</b><br>Held {c.duration:.2f}s — drag to change the hold "
                              "(snaps to half beats; Alt = free)", self)
            self.setCursor(Qt.SplitHCursor)
            return
        cid = self.hit_cue(pos)
        proj = self.s.project
        if cid:
            c = proj.cue(cid)
            tc = seconds_to_tc(c.time, proj.frame_rate, proj.tc_offset)
            tip = f"<b>{c.label or 'Cue'}</b><br>{tc}"
            if c.number is not None:
                tip += f"<br>Cue {c.number:g}"
            if c.source == "ai-accepted":
                tip += "<br><i>accepted AI suggestion</i>"
            if c.duration:
                tip += f"<br><b>Temp</b>: held {c.duration:.2f}s (Temp On → Temp Off)"
            if c.notes:
                tip += f"<br>{c.notes}"
            QToolTip.showText(e.globalPosition().toPoint(), tip, self)
            self.setCursor(Qt.SizeHorCursor)
            return
        sid = self.hit_suggestion(pos)
        if sid:
            s = proj.suggestion(sid)
            tc = seconds_to_tc(s.time, proj.frame_rate, proj.tc_offset)
            QToolTip.showText(e.globalPosition().toPoint(),
                              f"<b>Suggestion: {s.label or KIND_LABELS.get(s.kind, s.kind)}</b><br>{tc}"
                              f"<br>Confidence {s.confidence:.0%}<br>{s.reason}"
                              + (f"<br>Lasts {s.duration:.2f}s" if s.duration else "")
                              + (f"<br>{len(s.steps)} note steps (right-click: accept as chase steps)" if s.steps else "")
                              + (f"<br><b>Idea:</b> {s.idea}" if s.idea else "")
                              + "<br><i>Double-click or A to accept, X to reject</i>", self)
            self.setCursor(Qt.PointingHandCursor)
            return
        self.unsetCursor()
        QToolTip.hideText()

    # ------------------------------------------------------------- context menu
    def _section_menu(self, e) -> None:
        pos = e.position()
        s = self.s
        t = max(0.0, self.t_of(pos.x()))
        sec, _ = self.hit_section(pos)
        m = QMenu(self)
        m.addAction("Add section here", lambda: s.add_section_at(t))
        if sec is not None:
            s.select_section(sec.id)
            from ..core import arrange
            reps = arrange.repeats_of(s.project, sec)
            m.addAction(f"Rename '{sec.name}'…", lambda: self.rename_section_requested.emit(sec.id))
            m.addSeparator()
            if reps:
                names = ", ".join(r.name for r in reps)
                m.addAction(f"Copy cues to all repeats ({names})", lambda: s.copy_section_to_repeats(sec.id))
                m.addAction("Copy cues to all repeats, replacing their cues",
                            lambda: s.copy_section_to_repeats(sec.id, replace=True))
            sub = m.addMenu("Copy cues to section")
            for other in sorted(s.project.sections, key=lambda x: x.time):
                if other.id != sec.id:
                    sub.addAction(other.name, lambda oid=other.id: s.copy_section_to_repeats(sec.id, [oid]))
            m.addAction("Paste into this section (aligned)", lambda: s.paste_into_section(sec.id))
            m.addAction("Select cues in section", lambda: s.select_section_cues(sec.id))
            r = s.section_range(sec.id)
            m.addAction("Pattern fill this section…", lambda: self.pattern_fill_requested.emit(r[0], r[1]))
            m.addAction("Loop this section", lambda: self._loop_range(r))
            m.addSeparator()
            m.addAction(f"Delete '{sec.name}'", lambda: s.delete_section(sec.id))
        m.addSeparator()
        m.addAction("Create sections from AI suggestions", s.sections_from_ai)
        m.exec(e.globalPosition().toPoint())

    def _loop_range(self, r) -> None:
        self.s.engine.loop = (r[0], r[1])
        self.s.project.loop = (r[0], r[1])
        self.s.engine.loop_enabled = True
        self.s.status.emit("Looping section (L toggles loop)")
        self.invalidate()

    def _context_menu(self, e) -> None:
        pos = e.position()
        s = self.s
        if RULER_H <= pos.y() < TOP_H:
            self._section_menu(e)
            return
        m = QMenu(self)
        r = self.row_at(pos.y()) if pos.y() >= TOP_H else None
        t = max(0.0, self.t_of(pos.x()))
        cid = self.hit_cue(pos)
        sid = None if cid else self.hit_suggestion(pos)
        if cid and cid not in s.sel_cues:
            s.select(cues={cid})
        if sid and sid not in s.sel_sugs:
            s.select(sugs={sid})
        if r and r.kind == "lane":
            m.addAction("Add cue here", lambda: s.add_cue(r.id, t))
        if s.sel_cues:
            if cid:
                m.addAction("Edit cue…", lambda: self.edit_cue_requested.emit(cid))
            m.addAction(f"Delete {len(s.sel_cues)} cue(s)", s.delete_selected)
            m.addAction("Snap to grid", s.snap_selected)
            m.addAction(f"Make Temp (hold {s.temp_hold:g}s)", lambda: s.set_selected_temp(True))
            m.addAction("Make normal cue", lambda: s.set_selected_temp(False))
            sub = m.addMenu("Move to lane")
            for lane in s.project.lanes:
                sub.addAction(lane.name, lambda lid=lane.id: s.move_selected_to_lane(lid))
        if s.sel_sugs:
            m.addAction(f"Accept {len(s.sel_sugs)} suggestion(s)", lambda: s.accept(set(s.sel_sugs)))
            if sid and s.project.suggestion(sid) and s.project.suggestion(sid).steps:
                sg = s.project.suggestion(sid)
                m.addAction(f"Accept as chase steps ({len(sg.steps)} cues)", lambda: s.accept_as_steps(sid))
                if r and r.kind == "lane":
                    sub = m.addMenu("Accept as chase steps into lane")
                    for lane in s.project.lanes:
                        sub.addAction(lane.name, lambda lid=lane.id: s.accept_as_steps(sid, lid))
            m.addAction(f"Reject {len(s.sel_sugs)} suggestion(s)", lambda: s.reject(set(s.sel_sugs)))
        if r and r.kind == "lane":
            vis = [x.id for x in s.project.visible_suggestions() if x.lane_id == r.id]
            if vis:
                m.addAction(f"Accept all {len(vis)} visible suggestions in lane", lambda: s.accept(vis))
        if s.sel_cues:
            m.addAction(f"Copy {len(s.sel_cues)} cue(s)", s.copy_selected)
        if s.clipboard and not s.clipboard.empty:
            m.addAction(f"Paste {len(s.clipboard.cues)} cue(s) here", lambda: s.paste_at(t))
        m.addSeparator()
        m.addAction("Move playhead here", lambda: s.engine.seek(t))
        if s.project.beat_grid.beats:
            m.addAction("Make the nearest beat bar 1", lambda: s.set_downbeat_at(t))
            m.addAction("Make the nearest beat bar 1 and remove beats before it",
                        lambda: s.set_downbeat_at(t, drop_before=True))
        m.exec(e.globalPosition().toPoint())


class TimelinePanel(QWidget):
    """Canvas plus scrollbars."""

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.canvas = TimelineCanvas(session)
        self.hbar = QScrollBar(Qt.Horizontal)
        self.vbar = QScrollBar(Qt.Vertical)
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.canvas, 0, 0)
        lay.addWidget(self.vbar, 0, 1)
        lay.addWidget(self.hbar, 1, 0)
        self._updating = False
        self.canvas.view_changed.connect(self._sync_bars)
        for sig in (session.tracks_changed, session.lanes_changed, session.project_replaced):
            sig.connect(self._sync_bars)
        self.hbar.valueChanged.connect(self._h_moved)
        self.vbar.valueChanged.connect(self._v_moved)
        self.s = session

    def _sync_bars(self) -> None:
        self._updating = True
        c = self.canvas
        total_ms = int(self.s.duration * 1000)
        page = int(c.visible_seconds * 1000)
        self.hbar.setRange(-1000, max(0, total_ms - page // 2))
        self.hbar.setPageStep(page)
        self.hbar.setSingleStep(max(1, page // 20))
        self.hbar.setValue(int(c.t0 * 1000))
        extra = c.content_height() - (c.height() - TOP_H)
        self.vbar.setVisible(extra > 0)
        self.vbar.setRange(0, max(0, extra))
        self.vbar.setPageStep(max(1, c.height() - TOP_H))
        self.vbar.setValue(c.v_off)
        self._updating = False

    def _h_moved(self, v: int) -> None:
        if not self._updating:
            self.canvas.t0 = v / 1000
            self.canvas.invalidate()

    def _v_moved(self, v: int) -> None:
        if not self._updating:
            self.canvas.v_off = v
            self.canvas.invalidate()
