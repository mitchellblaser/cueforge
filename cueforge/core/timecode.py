"""SMPTE timecode conversion.

All times inside CueForge are float seconds. Timecode is only used for display,
input parsing and export.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FrameRate:
    key: str
    label: str
    fps: float          # real frames per second
    nominal: int        # frames counted per timecode second
    drop_frame: bool = False


FRAME_RATES: dict[str, FrameRate] = {
    "24": FrameRate("24", "24 fps", 24.0, 24),
    "25": FrameRate("25", "25 fps", 25.0, 25),
    "29.97df": FrameRate("29.97df", "29.97 fps drop-frame", 30000 / 1001, 30, True),
    "29.97": FrameRate("29.97", "29.97 fps non-drop", 30000 / 1001, 30),
    "30": FrameRate("30", "30 fps", 30.0, 30),
}
DEFAULT_RATE = "30"


def get_rate(key: str) -> FrameRate:
    return FRAME_RATES.get(key, FRAME_RATES[DEFAULT_RATE])


def seconds_to_frames(seconds: float, rate: FrameRate) -> int:
    """Nearest whole frame number for a time in seconds."""
    return int(math.floor(seconds * rate.fps + 0.5 + 1e-9))


def frames_to_seconds(frames: int, rate: FrameRate) -> float:
    return frames / rate.fps


def frame_count_to_tc(frames: int, rate: FrameRate) -> tuple[int, int, int, int]:
    """Convert an absolute frame count to (h, m, s, f) timecode labels."""
    neg = frames < 0
    frames = abs(frames)
    nom = rate.nominal
    if rate.drop_frame:
        drop = 2  # frames dropped per minute (except every 10th) for 29.97
        per_10min = nom * 600 - drop * 9
        per_min = nom * 60 - drop
        d, m = divmod(frames, per_10min)
        if m > drop:
            frames += drop * 9 * d + drop * ((m - drop) // per_min)
        else:
            frames += drop * 9 * d
    f = frames % nom
    s = (frames // nom) % 60
    mi = (frames // (nom * 60)) % 60
    h = frames // (nom * 3600)
    if neg:
        h = -h
    return h, mi, s, f


def tc_to_frame_count(h: int, m: int, s: int, f: int, rate: FrameRate) -> int:
    nom = rate.nominal
    total = ((h * 3600) + m * 60 + s) * nom + f
    if rate.drop_frame:
        total_minutes = h * 60 + m
        total -= 2 * (total_minutes - total_minutes // 10)
    return total


def seconds_to_tc(seconds: float, rate: FrameRate, offset: float = 0.0) -> str:
    h, m, s, f = frame_count_to_tc(seconds_to_frames(seconds + offset, rate), rate)
    sep = ";" if rate.drop_frame else ":"
    sign = "-" if seconds + offset < 0 and h == 0 else ""
    return f"{sign}{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"


_TC_RE = re.compile(r"^\s*(-)?(\d{1,2})[:.](\d{1,2})[:.](\d{1,2})[:;.](\d{1,2})\s*$")


def parse_tc(text: str, rate: FrameRate, offset: float = 0.0) -> float:
    """Parse 'HH:MM:SS:FF' (or plain seconds) into song-relative seconds."""
    text = text.strip()
    m = _TC_RE.match(text)
    if not m:
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError(f"Not a timecode: {text!r}") from exc
    neg, h, mi, s, f = m.groups()
    h, mi, s, f = int(h), int(mi), int(s), int(f)
    if mi > 59 or s > 59 or f >= rate.nominal:
        raise ValueError(f"Timecode out of range: {text!r}")
    secs = frames_to_seconds(tc_to_frame_count(h, mi, s, f, rate), rate)
    if neg:
        secs = -secs
    return secs - offset


def snap_to_frame(seconds: float, rate: FrameRate) -> float:
    return frames_to_seconds(seconds_to_frames(seconds, rate), rate)


def format_seconds(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    m, s = divmod(seconds, 60)
    return f"{sign}{int(m)}:{s:06.3f}"
