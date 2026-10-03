"""Code paths for the optional deep-learning backends, with stand-in models.

Real model weights are downloaded on first use; these tests check our glue code
(shapes, resampling, fallbacks) without network access.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge.analysis import beats as beats_mod
from cueforge.analysis import stems


def test_beat_this_glue(monkeypatch):
    pytest.importorskip("beat_this")
    import beat_this.inference as bti

    class Fake:
        def __init__(self, **kw):
            pass

        def __call__(self, signal, sr):
            b = np.arange(0, 10, 0.5)
            return b, b[::4]

    monkeypatch.setattr(bti, "Audio2Beats", Fake)
    g = beats_mod.detect_beats(np.zeros(22050 * 10, np.float32), 22050, use_deep=True)
    assert g.source == "Beat This!" and g.beats_per_bar == 4 and abs(g.bpm() - 120) < 1e-6
    assert not g.confirmed


def test_beat_this_failure_falls_back(monkeypatch):
    pytest.importorskip("beat_this")
    import beat_this.inference as bti

    class Broken:
        def __init__(self, **kw):
            raise RuntimeError("no network")

    monkeypatch.setattr(bti, "Audio2Beats", Broken)
    sr = 22050
    y = np.zeros(sr * 10, np.float32)
    for k in range(20):
        i = int(k * 0.5 * sr)
        y[i:i + 200] = 0.8
    g = beats_mod.detect_beats(y, sr, use_deep=True)
    assert g.source == "librosa"


def test_demucs_glue(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")
    pytest.importorskip("demucs")
    import demucs.apply
    import demucs.pretrained

    class FakeModel:
        samplerate = 44100
        sources = ["drums", "bass", "other", "vocals"]

        def eval(self):
            return self

    def fake_apply(model, mix, **kw):
        assert mix.shape[0] == 1 and mix.shape[1] == 2
        return torch.stack([mix[0] * (i + 1) for i in range(4)])[None]

    monkeypatch.setattr(demucs.pretrained, "get_model", lambda name: FakeModel())
    monkeypatch.setattr(demucs.apply, "apply_model", fake_apply)
    y = (np.random.default_rng(0).standard_normal((48000, 2)) * 0.1).astype(np.float32)
    out = stems.separate(y, 48000, str(tmp_path))
    assert set(out) == {"drums", "bass", "other", "vocals"}
    assert abs(len(out["drums"]) - 48000) < 10
    # cached on disk: second call does not touch the model
    monkeypatch.setattr(demucs.pretrained, "get_model", lambda name: 1 / 0)
    again = stems.separate(y, 48000, str(tmp_path))
    assert np.allclose(again["vocals"], out["vocals"])
