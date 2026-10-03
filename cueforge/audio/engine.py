"""Real-time multitrack playback and mixing.

`AudioEngine.render()` is a pure mixing function used both by the audio callback
and by tests/offline bouncing. If no audio device is available the engine runs on
a wall-clock thread so the UI still works (silently).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from .loader import ENGINE_SR
from .sounds import blip_sound, click_sounds

BLOCK = 512


def db_to_gain(db: float) -> float:
    return 0.0 if db <= -60 else float(10 ** (db / 20))


@dataclass
class EngineTrack:
    id: str
    samples: np.ndarray       # (n, 2) float32
    offset: float = 0.0       # seconds
    gain: float = 1.0
    mute: bool = False
    solo: bool = False


class AudioEngine:
    def __init__(self, sr: int = ENGINE_SR) -> None:
        self.sr = sr
        self._lock = threading.RLock()
        self.tracks: dict[str, EngineTrack] = {}
        self.master_gain = 1.0
        self.speed = 1.0
        self.loop: tuple[float, float] | None = None
        self.loop_enabled = False
        self.duration = 0.0

        self._accent, self._normal = click_sounds(sr)
        self._blip = blip_sound(sr)
        self.click_enabled = False
        self.click_gain = db_to_gain(-6)
        self._beats = np.zeros(0)
        self._beat_accent = np.zeros(0, bool)
        self.blips_enabled = False
        self.blips_gain = db_to_gain(-10)
        self._blip_times = np.zeros(0)

        self.meters: dict[str, float] = {}
        self.master_meter = (0.0, 0.0)

        self._playing = False
        self._pos = 0.0                 # song seconds at start of next block
        self._cb_pos = 0.0
        self._cb_wall = time.perf_counter()
        self.latency = 0.0

        self.device = None              # sounddevice device index/name or None for default
        self._stream = None
        self._fallback_thread: threading.Thread | None = None
        self._fallback_stop = threading.Event()
        self.backend = "none"
        self.error = ""
        # scrubbing (audio while dragging the playhead with playback stopped)
        self._scrub_buf = np.zeros((0, 2), np.float32)
        self._scrub_last: tuple[float, float] | None = None   # (song time, wall time)

    # -- configuration --------------------------------------------------
    def set_track(self, track_id: str, samples: np.ndarray) -> None:
        with self._lock:
            old = self.tracks.get(track_id)
            self.tracks[track_id] = EngineTrack(track_id, samples, *(
                (old.offset, old.gain, old.mute, old.solo) if old else ()))
            self._update_duration()

    def remove_track(self, track_id: str) -> None:
        with self._lock:
            self.tracks.pop(track_id, None)
            self.meters.pop(track_id, None)
            self._update_duration()

    def update_track(self, track_id: str, *, gain_db: float | None = None, mute: bool | None = None,
                     solo: bool | None = None, offset: float | None = None) -> None:
        with self._lock:
            t = self.tracks.get(track_id)
            if not t:
                return
            if gain_db is not None:
                t.gain = db_to_gain(gain_db)
            if mute is not None:
                t.mute = mute
            if solo is not None:
                t.solo = solo
            if offset is not None:
                t.offset = offset
                self._update_duration()

    def _update_duration(self) -> None:
        self.duration = max((t.offset + len(t.samples) / self.sr for t in self.tracks.values()), default=0.0)

    def set_click(self, beats: list[float], downbeats: list[float], enabled: bool, db: float) -> None:
        with self._lock:
            self._beats = np.asarray(beats, float)
            db_set = np.asarray(downbeats, float)
            if len(db_set) and len(self._beats):
                self._beat_accent = np.min(np.abs(self._beats[:, None] - db_set[None, :]), axis=1) < 1e-3
            else:
                self._beat_accent = np.zeros(len(self._beats), bool)
            self.click_enabled = enabled
            self.click_gain = db_to_gain(db)

    def set_blips(self, times: list[float], enabled: bool, db: float) -> None:
        with self._lock:
            self._blip_times = np.sort(np.asarray(times, float))
            self.blips_enabled = enabled
            self.blips_gain = db_to_gain(db)

    # -- mixing ---------------------------------------------------------
    def render(self, start: float, frames: int, speed: float = 1.0) -> np.ndarray:
        out = np.zeros((frames, 2), np.float32)
        step = speed / self.sr
        any_solo = any(t.solo for t in self.tracks.values())
        for t in self.tracks.values():
            audible = not t.mute and (t.solo or not any_solo)
            seg = self._track_segment(t, start, frames, speed)
            peak = 0.0
            if seg is not None and audible and t.gain > 0:
                seg = seg * t.gain
                out += seg
                peak = float(np.abs(seg).max()) if len(seg) else 0.0
            self.meters[t.id] = peak
        if self.click_enabled and len(self._beats):
            self._add_events(out, start, step, self._beats, self.click_gain, self._beat_accent)
        if self.blips_enabled and len(self._blip_times):
            self._add_events(out, start, step, self._blip_times, self.blips_gain, None)
        out *= self.master_gain
        self.master_meter = (float(np.abs(out[:, 0]).max()), float(np.abs(out[:, 1]).max()))
        np.clip(out, -1.0, 1.0, out=out)
        return out

    def _track_segment(self, t: EngineTrack, start: float, frames: int, speed: float) -> np.ndarray | None:
        n = len(t.samples)
        src0 = (start - t.offset) * self.sr
        if speed == 1.0:
            i0 = int(round(src0))
            a, b = max(i0, 0), min(i0 + frames, n)
            if b <= a:
                return None
            seg = np.zeros((frames, 2), np.float32)
            seg[a - i0:b - i0] = t.samples[a:b]
            return seg
        pos = src0 + np.arange(frames) * speed
        valid = (pos >= 0) & (pos < n - 1)
        if not valid.any():
            return None
        seg = np.zeros((frames, 2), np.float32)
        p = pos[valid]
        i = p.astype(np.int64)
        frac = (p - i)[:, None].astype(np.float32)
        seg[valid] = t.samples[i] * (1 - frac) + t.samples[i + 1] * frac
        return seg

    def _add_events(self, out: np.ndarray, start: float, step: float, times: np.ndarray,
                    gain: float, accents: np.ndarray | None) -> None:
        frames = len(out)
        tail = max(len(self._accent), len(self._blip)) * step
        end = start + frames * step
        lo = np.searchsorted(times, start - tail)
        hi = np.searchsorted(times, end)
        for j in range(lo, hi):
            if accents is None:
                snd = self._blip
            else:
                snd = self._accent if accents[j] else self._normal
            k0 = int(round((times[j] - start) / step))
            s0 = max(0, -k0)
            o0 = max(0, k0)
            m = min(len(snd) - s0, frames - o0)
            if m > 0:
                out[o0:o0 + m] += snd[s0:s0 + m] * gain

    # -- transport ------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self._playing

    def position(self) -> float:
        with self._lock:
            if not self._playing:
                return self._pos
            p = self._cb_pos + (time.perf_counter() - self._cb_wall) * self.speed
            return max(0.0, p)

    def seek(self, t: float) -> None:
        with self._lock:
            self._pos = max(0.0, t)
            self._cb_pos = self._pos
            self._cb_wall = time.perf_counter() + self.latency

    def play(self) -> None:
        with self._lock:
            if self._playing:
                return
            if self.loop_enabled and self.loop and not (self.loop[0] <= self._pos < self.loop[1]):
                self._pos = self.loop[0]
            self._cb_pos = self._pos
            self._cb_wall = time.perf_counter() + self.latency
            self._playing = True
        self._ensure_output()

    def pause(self) -> None:
        with self._lock:
            if not self._playing:
                return
            pos = self.position()
            self._playing = False
            self._pos = pos
            for k in self.meters:
                self.meters[k] = 0.0
            self.master_meter = (0.0, 0.0)

    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    GRAIN = 0.07   # seconds of audio per scrub grain

    def scrub_grain(self, t: float, speed: float = 1.0) -> np.ndarray:
        """A short, faded slice of the mix at song time t (reversed for negative speed)."""
        n = int(self.GRAIN * self.sr)
        sp = max(0.25, min(4.0, abs(speed)))
        start = t if speed >= 0 else max(0.0, t - n * sp / self.sr)
        g = self.render(start, n, sp)
        if speed < 0:
            g = g[::-1].copy()
        fade_in, fade_out = int(0.004 * self.sr), int(0.02 * self.sr)
        g[:fade_in] *= np.linspace(0, 1, fade_in, dtype=np.float32)[:, None]
        g[-fade_out:] *= np.linspace(1, 0, fade_out, dtype=np.float32)[:, None]
        return g

    def scrub(self, t: float) -> None:
        """Called while the playhead is dragged: queue a grain whose speed follows the drag."""
        if self._playing:
            return
        now = time.perf_counter()
        speed = 1.0
        if self._scrub_last is not None:
            lt, lw = self._scrub_last
            dw = now - lw
            if 0.005 < dw < 0.25:
                speed = (t - lt) / dw
                if abs(speed) < 0.25:
                    speed = 0.25 if speed >= 0 else -0.25
        self._scrub_last = (t, now)
        with self._lock:
            grain = self.scrub_grain(t, speed)
            self.meters = {k: 0.0 for k in self.meters}
            self._scrub_buf = grain
        self._ensure_output()

    def _next_block(self, frames: int) -> np.ndarray:
        with self._lock:
            if not self._playing:
                out = np.zeros((frames, 2), np.float32)
                if len(self._scrub_buf):
                    m = min(frames, len(self._scrub_buf))
                    out[:m] = self._scrub_buf[:m]
                    self._scrub_buf = self._scrub_buf[m:]
                return out
            out = np.zeros((frames, 2), np.float32)
            done = 0
            while done < frames:
                n = frames - done
                if self.loop_enabled and self.loop and self.loop[1] > self.loop[0]:
                    remaining = (self.loop[1] - self._pos) / (self.speed / self.sr)
                    if remaining <= 0:
                        self._pos = self.loop[0]
                        continue
                    n = min(n, max(1, int(remaining)))
                out[done:done + n] = self.render(self._pos, n, self.speed)
                self._pos += n * self.speed / self.sr
                done += n
            self._cb_pos = self._pos - frames * self.speed / self.sr
            self._cb_wall = time.perf_counter() + self.latency
            if not self.loop_enabled and self.duration and self._pos > self.duration + 0.5:
                self._playing = False
                self._pos = self.duration
            return out

    # -- output ---------------------------------------------------------
    def _ensure_output(self) -> None:
        if self._stream is not None or (self._fallback_thread and self._fallback_thread.is_alive()):
            return
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(samplerate=self.sr, channels=2, dtype="float32",
                                           blocksize=BLOCK, device=self.device, latency="low",
                                           callback=self._callback)
            self._stream.start()
            self.latency = float(self._stream.latency or 0.0)
            self.backend = "sounddevice"
            self.error = ""
        except Exception as exc:  # no device / no PortAudio
            self._stream = None
            self.error = str(exc)
            self.backend = "silent"
            self.latency = 0.0
            self._fallback_stop.clear()
            self._fallback_thread = threading.Thread(target=self._fallback_loop, daemon=True)
            self._fallback_thread.start()

    def _callback(self, outdata, frames, time_info, status) -> None:  # pragma: no cover - realtime
        outdata[:] = self._next_block(frames)

    def _fallback_loop(self) -> None:
        period = BLOCK / self.sr
        nxt = time.perf_counter()
        while not self._fallback_stop.is_set():
            self._next_block(BLOCK)
            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()

    def set_device(self, device) -> None:
        was_playing = self._playing
        self.pause()
        self.close()
        self.device = device
        if was_playing:
            self.play()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._fallback_stop.set()
        if self._fallback_thread:
            self._fallback_thread.join(timeout=1)
            self._fallback_thread = None

    def bounce(self, start: float = 0.0, end: float | None = None) -> np.ndarray:
        """Offline mixdown (used for tests and 'export mix')."""
        end = self.duration if end is None else end
        frames = int((end - start) * self.sr)
        return self.render(start, frames, 1.0)


def list_output_devices() -> list[tuple[int, str]]:
    try:
        import sounddevice as sd
        return [(i, f"{d['name']} ({sd.query_hostapis(d['hostapi'])['name']})")
                for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] >= 2]
    except Exception:
        return []
