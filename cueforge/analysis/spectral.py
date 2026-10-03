"""Shared spectral analysis so detectors don't each redo the STFT / HPSS."""
from __future__ import annotations

import numpy as np

N_FFT = 2048
HOP = 256


class Spectra:
    """Lazily computed STFT and harmonic/percussive split of one signal."""

    def __init__(self, y: np.ndarray, sr: int, kernel: int = 17) -> None:
        self.y, self.sr, self.kernel = y, sr, kernel
        self._D = None
        self._med = None
        self._harm_audio: dict[float, np.ndarray] = {}

    @property
    def D(self) -> np.ndarray:
        if self._D is None:
            import librosa
            self._D = librosa.stft(self.y, n_fft=N_FFT, hop_length=HOP)
        return self._D

    @property
    def S(self) -> np.ndarray:
        return np.abs(self.D)

    def _medians(self):
        if self._med is None:
            from scipy.ndimage import median_filter
            S = self.S
            self._med = (median_filter(S, size=(1, self.kernel), mode="reflect"),
                         median_filter(S, size=(self.kernel, 1), mode="reflect"))
        return self._med

    def percussive(self, margin: float = 2.0) -> np.ndarray:
        """Percussive magnitude spectrogram."""
        import librosa
        H, P = self._medians()
        mask = librosa.util.softmask(P, margin * H, power=2.0)
        return self.S * mask

    def harmonic_audio(self, margin: float = 2.0) -> np.ndarray:
        if margin not in self._harm_audio:
            import librosa
            H, P = self._medians()
            mask = librosa.util.softmask(H, margin * P, power=2.0)
            self._harm_audio[margin] = librosa.istft(self.D * mask, hop_length=HOP, length=len(self.y))
        return self._harm_audio[margin]
