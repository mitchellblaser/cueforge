"""Optional source separation with Demucs (cached on disk)."""
from __future__ import annotations

import hashlib
import os

import numpy as np


def demucs_available() -> bool:
    try:
        import demucs  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def separate(y_stereo: np.ndarray, sr: int, cache_dir: str | None = None,
             progress=None) -> dict[str, np.ndarray] | None:
    """Return {stem: mono float32 at sr} or None if Demucs is unavailable/failed."""
    key = hashlib.sha1(np.ascontiguousarray(y_stereo[:: max(1, len(y_stereo) // 200000)]).tobytes()).hexdigest()[:16]
    cache = os.path.join(cache_dir, f"stems_{key}.npz") if cache_dir else None
    if cache and os.path.exists(cache):
        with np.load(cache) as z:
            return {k: z[k] for k in z.files}
    if not demucs_available():
        return None
    try:
        import torch
        from demucs.apply import apply_model
        from demucs.pretrained import get_model
        from ..audio.loader import resample
        model = get_model("htdemucs")
        model.eval()
        msr = model.samplerate
        x = resample(y_stereo.astype(np.float32), sr, msr)
        wav = torch.from_numpy(x.T.copy())
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        wav = (wav - mean) / std
        with torch.no_grad():
            sources = apply_model(model, wav[None], device="cpu", split=True, overlap=0.25,
                                  progress=False)[0]
        sources = sources * std + mean
        out = {}
        for name, src in zip(model.sources, sources):
            mono = src.mean(0).numpy().astype(np.float32)
            out[name] = resample(mono[:, None], msr, sr)[:, 0]
        if cache:
            np.savez_compressed(cache, **out)
        return out
    except Exception:
        return None
