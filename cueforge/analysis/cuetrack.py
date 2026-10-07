"""Sections from a spoken cue track ("Verse… 3, 4", "Chorus", "Bridge").

Backing-track rigs usually carry a guide track where a voice announces each section a bar
before it starts. That's far more reliable than guessing sections from the music:

1. Calls are found by level alone (a word plus any count-in, gaps under ~1 s), so the timing
   never depends on recognising the word. The section starts on the first downbeat after
   the call.
2. Each call is named by a small keyword spotter (sherpa-onnx, optional AI model) listening
   for section words only.
3. Calls it can't name take the name of a call that sounds the same (MFCC + DTW), or a letter
   ("Cue A", "Cue B" …) so repeats still line up.

A track that's mostly sound (a guide vocal or instrument guide rather than spoken cues) is
left alone.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

# section words and their tokens for the keyword model (BPE of its bpe.model, made once)
VOCAB = [
    ("Intro", "▁IN T RO"), ("Verse", "▁ VER SE"), ("Verse 1", "▁ VER SE ▁ONE"),
    ("Verse 2", "▁ VER SE ▁TWO"), ("Verse 3", "▁ VER SE ▁THREE"), ("Verse 4", "▁ VER SE ▁FOUR"),
    ("Pre-Chorus", "▁PRE ▁C HO RU S"), ("Chorus", "▁C HO RU S"), ("Chorus 2", "▁C HO RU S ▁TWO"),
    ("Last Chorus", "▁LAST ▁C HO RU S"), ("Final Chorus", "▁F IN AL ▁C HO RU S"),
    ("Post-Chorus", "▁PO S T ▁C HO RU S"), ("Bridge", "▁B RI D GE"), ("Breakdown", "▁B RE A K D OW N"),
    ("Instrumental", "▁IN S T RU MENT AL"), ("Interlude", "▁IN TER LU DE"), ("Solo", "▁SO LO"),
    ("Guitar Solo", "▁GU IT AR ▁SO LO"), ("Tag", "▁TA G"), ("Outro", "▁OUT RO"), ("Ending", "▁EN D ING"),
    ("Turnaround", "▁TURN AR O UN D"), ("Refrain", "▁RE F RA IN"), ("Vamp", "▁VA M P"), ("Hook", "▁HO O K"),
    ("Drop", "▁DR O P"), ("Build", "▁BUILD"), ("Break", "▁B RE A K"), ("All In", "▁ALL ▁IN"),
    ("Key Change", "▁K E Y ▁CHANGE"), ("A Cappella", "▁A ▁CA PP EL LA"), ("Hits", "▁HI T S"),
    ("Drums", "▁DR UM S"), ("Down", "▁DOWN"), ("Swell", "▁S W E LL"), ("Rap", "▁RA P"),
    ("Exhale", "▁EX HA LE"), ("Big Ending", "▁B IG ▁EN D ING"), ("Middle Eight", "▁MI D D LE ▁E IGHT"),
]
KWS_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
           "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2")
KWS_DIR = "kws-gigaspeech-3.3M"
KWS_FILES = {"encoder": "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
             "decoder": "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
             "joiner": "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
             "tokens": "tokens.txt"}

FRAME = 0.01            # seconds per level frame
JOIN_GAP = 0.9          # utterances closer than this belong to one call (name + count-in)
MAX_SPEECH_SHARE = 0.35  # more sound than this: a guide vocal / instrument track, not spoken cues


@dataclass
class Call:
    start: float
    end: float
    parts: list[tuple[float, float]] = field(default_factory=list)
    name: str = ""
    recognised: bool = False


@dataclass
class SpokenSection:
    time: float
    label: str
    confidence: float
    reason: str


def _levels(y: np.ndarray, sr: int) -> np.ndarray:
    hop = int(FRAME * sr)
    n = len(y) // hop
    if n == 0:
        return np.zeros(0)
    fr = y[:n * hop].reshape(n, hop)
    return 20 * np.log10(np.sqrt((fr.astype(np.float64) ** 2).mean(axis=1)) + 1e-9)


def find_calls(y: np.ndarray, sr: int) -> tuple[list[Call], float]:
    """Spoken calls (utterances grouped with their count-in) and the share of the track
    that has sound at all."""
    db = _levels(y, sr)
    if not len(db) or db.max() < -60:
        return [], 0.0
    floor = float(np.percentile(db, 10))
    thr = max(floor + 15, db.max() - 35)
    on = db > thr
    share = float(on.mean())
    # utterances: runs of sound, short dropouts (between syllables) bridged
    utts: list[tuple[float, float]] = []
    i, n = 0, len(on)
    bridge = int(0.15 / FRAME)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j:j + bridge].any():
            j += 1
        if (j - i) * FRAME >= 0.08:
            utts.append((i * FRAME, j * FRAME))
        i = j
    calls: list[Call] = []
    for a, b in utts:
        if calls and a - calls[-1].end < JOIN_GAP:
            calls[-1].end = b
            calls[-1].parts.append((a, b))
        else:
            calls.append(Call(a, b, [(a, b)]))
    return calls, share


# --------------------------------------------------------------------- naming
def spotter_dir() -> str:
    from .. import addons
    return os.path.join(addons.models_dir(), KWS_DIR)


def spotter_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        return False


def spotter_available() -> bool:
    return spotter_installed() and all(os.path.exists(os.path.join(spotter_dir(), f)) for f in KWS_FILES.values())


def _ssl_context():
    import ssl
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):     # e.g. a company proxy's certificates
        if os.environ.get(var) and os.path.exists(os.environ[var]):
            return ssl.create_default_context(cafile=os.environ[var])
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:                                     # the packaged app always has pip's copy
        from pip._vendor import certifi as pip_certifi
        return ssl.create_default_context(cafile=pip_certifi.where())
    except Exception:
        return ssl.create_default_context()


def download_spotter(log=print) -> bool:
    """Fetch the keyword model (~17 MB download, ~5 MB kept) into the models folder."""
    import io
    import tarfile
    import urllib.request
    d = spotter_dir()
    os.makedirs(d, exist_ok=True)
    log("Downloading the spoken-cue keyword model…")
    with urllib.request.urlopen(KWS_URL, timeout=120, context=_ssl_context()) as r:
        data = r.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tar:
        for m in tar.getmembers():
            base = os.path.basename(m.name)
            if base in KWS_FILES.values():
                f = tar.extractfile(m)
                if f is not None:
                    with open(os.path.join(d, base), "wb") as out:
                        out.write(f.read())
    ok = all(os.path.exists(os.path.join(d, f)) for f in KWS_FILES.values())
    log("Keyword model ready" if ok else "Keyword model download incomplete")
    return ok


class Spotter:
    """Listens for section words only (open-vocabulary keyword spotting)."""

    def __init__(self) -> None:
        import sherpa_onnx
        d = spotter_dir()
        self._kw = os.path.join(d, "cueforge-keywords.txt")
        with open(self._kw, "w", encoding="utf-8") as f:
            for name, toks in VOCAB:
                f.write(f"{toks} @{name.replace(' ', '_')}\n")
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=os.path.join(d, KWS_FILES["tokens"]), encoder=os.path.join(d, KWS_FILES["encoder"]),
            decoder=os.path.join(d, KWS_FILES["decoder"]), joiner=os.path.join(d, KWS_FILES["joiner"]),
            keywords_file=self._kw, num_threads=2, keywords_score=3.0, keywords_threshold=0.05,
            max_active_paths=4)

    def words(self, y16: np.ndarray) -> list[str]:
        """Section words heard in this stretch of the track (with some of the track around it)."""
        s = self.kws.create_stream()
        x = np.concatenate([y16.astype(np.float32), np.zeros(int(0.6 * 16000), np.float32)])
        found = []
        for i in range(0, len(x), 1600):
            s.accept_waveform(16000, x[i:i + 1600])
            while self.kws.is_ready(s):
                self.kws.decode_stream(s)
                r = self.kws.get_result(s)
                if r:
                    found.append(r.replace("_", " "))
                    self.kws.reset_stream(s)
        s.input_finished()
        while self.kws.is_ready(s):
            self.kws.decode_stream(s)
            r = self.kws.get_result(s)
            if r:
                found.append(r.replace("_", " "))
                self.kws.reset_stream(s)
        return found


def _fingerprint(y: np.ndarray, sr: int, call: Call) -> np.ndarray | None:
    import librosa
    a, b = call.parts[0]                              # the word itself, not the count-in
    seg = y[int(a * sr):int(b * sr)]
    if len(seg) < int(0.05 * sr):
        return None
    m = librosa.feature.mfcc(y=seg, sr=sr, n_mfcc=13, hop_length=int(0.01 * sr), n_fft=int(0.025 * sr) * 2)
    m = m[1:]                                         # drop overall level
    return (m - m.mean(axis=1, keepdims=True)) / (m.std(axis=1, keepdims=True) + 1e-6)


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    import librosa
    D, wp = librosa.sequence.dtw(X=a, Y=b, metric="euclidean")
    return float(D[-1, -1] / len(wp))


def name_calls(y: np.ndarray, sr: int, calls: list[Call], spotter: Spotter | None) -> None:
    import librosa
    if spotter is not None:
        y16 = librosa.resample(y, orig_sr=sr, target_sr=16000)
        for c in calls:
            a, b = c.parts[0]
            words = spotter.words(y16[int(max(0.0, a - 0.5) * 16000):int((b + 0.4) * 16000)])
            if words:
                c.name = max(words, key=len)              # "Pre-Chorus" over "Break"
                c.recognised = True
    # unnamed calls: the name of a call that sounds the same, else a letter per kind of call
    prints = [_fingerprint(y, sr, c) for c in calls]
    valid = [p for p in prints if p is not None]
    pair = [_distance(a, b) for i, a in enumerate(valid) for b in valid[i + 1:]]
    same = 0.65 * float(np.median(pair)) if pair else 0.0    # "sounds the same" for this voice
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    groups: list[tuple[np.ndarray, str]] = []
    for c, fp in zip(calls, prints):
        if c.recognised and fp is not None:
            groups.append((fp, c.name))
    for c, fp in zip(calls, prints):
        if c.recognised or fp is None:
            continue
        best = min(((_distance(fp, g), name) for g, name in groups), default=(9.0, ""))
        if best[0] < same:
            c.name = best[1]
        else:
            c.name = f"Cue {next(letters, '?')}"
            groups.append((fp, c.name))


def detect_spoken_sections(y: np.ndarray, sr: int, downbeats: list[float], beats: list[float],
                           use_spotter: bool = True, log=None) -> list[SpokenSection]:
    calls, share = find_calls(y, sr)
    say = log or (lambda m: None)
    if len(calls) < 2:
        say("Cue track: no spoken cues found")
        return []
    if share > MAX_SPEECH_SHARE:
        say(f"Cue track: sound {share:.0%} of the time, so it's a guide part rather than spoken cues: skipped")
        return []
    spotter = None
    if use_spotter and spotter_installed() and not spotter_available():
        try:                                  # installed, model not fetched yet (offline before?)
            download_spotter(say)
        except Exception as exc:
            say(f"Cue track: keyword model not downloaded ({exc}); naming calls by sound only")
    if use_spotter and spotter_available():
        try:
            spotter = Spotter()
        except Exception as exc:
            say(f"Cue track: keyword model could not load ({exc}); naming calls by sound only")
    name_calls(y, sr, calls, spotter)
    db = np.asarray(downbeats, float)
    ibi = float(np.median(np.diff(beats))) if len(beats) > 1 else 0.5
    out: list[SpokenSection] = []
    for c in calls:
        if len(db):
            later = db[db > c.end - 0.15]
            if not len(later):
                continue
            t = float(later[0])
            if t - c.end > 4 * ibi * 2:                 # nothing near: not a section call
                continue
        else:
            t = c.end + 0.25
        conf = 0.95 if c.recognised else 0.8
        how = f"\"{c.name}\" spoken on the cue track" if c.recognised else \
            f"call on the cue track (sounds like {c.name})" if not c.name.startswith("Cue ") else \
            "call on the cue track"
        out.append(SpokenSection(round(t, 6), c.name, conf, f"{how}, section starts on the next downbeat"))
    named = sum(1 for c in calls if c.recognised)
    say(f"Cue track: {len(out)} spoken cues ({named} recognised by word)")
    return out
