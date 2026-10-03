"""Audio decoding, resampling and waveform peak generation."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

import numpy as np

ENGINE_SR = 48000
ANALYSIS_SR = 22050
AUDIO_EXTENSIONS = (".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a", ".aac")
PEAK_BLOCK = 256  # samples per peak at the finest level


@dataclass
class PeakPyramid:
    """Min/max envelopes at several resolutions for fast waveform drawing."""
    sr: int
    levels: list[tuple[int, np.ndarray, np.ndarray]] = field(default_factory=list)  # (block, mins, maxs)

    @classmethod
    def build(cls, mono: np.ndarray, sr: int) -> "PeakPyramid":
        levels = []
        block = PEAK_BLOCK
        n = len(mono) // block
        if n == 0:
            mono = np.pad(mono, (0, block - len(mono)))
            n = 1
        frames = mono[: n * block].reshape(n, block)
        mins, maxs = frames.min(axis=1), frames.max(axis=1)
        levels.append((block, mins.astype(np.float32), maxs.astype(np.float32)))
        while len(mins) > 64:
            m = len(mins) // 4
            if m == 0:
                break
            mins = mins[: m * 4].reshape(m, 4).min(axis=1)
            maxs = maxs[: m * 4].reshape(m, 4).max(axis=1)
            block *= 4
            levels.append((block, mins.astype(np.float32), maxs.astype(np.float32)))
        return cls(sr, levels)

    def envelope(self, t0: float, t1: float, columns: int) -> tuple[np.ndarray, np.ndarray]:
        """Return (mins, maxs) arrays of length `columns` covering [t0, t1)."""
        columns = max(1, columns)
        spp = (t1 - t0) * self.sr / columns  # samples per pixel
        level = self.levels[0]
        for lv in self.levels:
            if lv[0] <= spp:
                level = lv
        block, mins, maxs = level
        edges = (t0 * self.sr + np.arange(columns + 1) * spp) / block
        idx = np.floor(edges).astype(np.int64)
        out_min = np.zeros(columns, np.float32)
        out_max = np.zeros(columns, np.float32)
        n = len(mins)
        a = np.clip(idx[:-1], 0, n)
        b = np.clip(np.maximum(idx[1:], idx[:-1] + 1), 0, n)
        valid = a < b
        if valid.any() and n:
            if spp >= block:
                # reduceat over the valid, monotonic ranges
                starts = a[valid]
                rmin = np.minimum.reduceat(mins, np.minimum(starts, n - 1))
                rmax = np.maximum.reduceat(maxs, np.minimum(starts, n - 1))
                # reduceat runs the last range to the end of the array; clamp it
                la, lb = int(starts[-1]), int(b[valid][-1])
                if lb > la:
                    rmin[-1], rmax[-1] = mins[la:lb].min(), maxs[la:lb].max()
                out_min[valid], out_max[valid] = rmin, rmax
            else:
                out_min[valid], out_max[valid] = mins[a[valid]], maxs[a[valid]]
        return out_min, out_max


@dataclass
class AudioData:
    path: str
    sr: int
    samples: np.ndarray        # float32, shape (n, 2), at ENGINE_SR
    peaks: PeakPyramid
    original_sr: int = 0
    original_channels: int = 0

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sr

    def mono(self) -> np.ndarray:
        return self.samples.mean(axis=1)


class AudioLoadError(Exception):
    pass


def _decode_soundfile(path: str) -> tuple[np.ndarray, int]:
    import soundfile as sf
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data, sr


def _decode_ffmpeg(path: str) -> tuple[np.ndarray, int]:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise AudioLoadError("ffmpeg not found (needed for this file type)")
    sr = ENGINE_SR
    proc = subprocess.run(
        [ff, "-v", "error", "-i", path, "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "2", "-ar", str(sr), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise AudioLoadError(proc.stderr.decode(errors="replace").strip() or "ffmpeg failed")
    data = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2)
    return data, sr


def resample(data: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return data
    try:
        import soxr
        return soxr.resample(data, sr_in, sr_out, quality="HQ").astype(np.float32)
    except ImportError:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(sr_in, sr_out)
        return resample_poly(data, sr_out // g, sr_in // g, axis=0).astype(np.float32)


def load_audio(path: str, sr: int = ENGINE_SR) -> AudioData:
    if not os.path.exists(path):
        raise AudioLoadError(f"File not found: {path}")
    try:
        data, file_sr = _decode_soundfile(path)
    except Exception:
        data, file_sr = _decode_ffmpeg(path)
    channels = data.shape[1]
    if channels == 1:
        data = np.repeat(data, 2, axis=1)
    elif channels > 2:
        data = data[:, :2]
    data = resample(np.ascontiguousarray(data, dtype=np.float32), file_sr, sr)
    data = np.ascontiguousarray(data, dtype=np.float32)
    peaks = PeakPyramid.build(data.mean(axis=1), sr)
    return AudioData(path, sr, data, peaks, file_sr, channels)


def to_analysis_mono(audio: AudioData, sr: int = ANALYSIS_SR) -> np.ndarray:
    return resample(audio.mono()[:, None], audio.sr, sr)[:, 0]
