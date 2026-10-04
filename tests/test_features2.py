"""Bar 1 / gaps / pauses, CuePoints import, bulk import, progress model, AI add-ons."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge import addons
from cueforge.analysis.beats import detect_beats_librosa, internal_gaps, music_bounds
from cueforge.core.bulk import song_groups_from_files, song_groups_from_folder
from cueforge.core.grid_tools import drop_beats_before_bar_one, halve_tempo, move_bar_one, set_bar_one
from cueforge.core.model import BeatGrid, Project
from cueforge.export.cuepoints_import import (ImportOptions, guess_mapping, import_rows, parse_time, read_table,
                                              rows)
from cueforge.ui.analysis_runner import ProgressModel
from tests.synth import SR, make_song


# ------------------------------------------------------------------ bar 1
def _grid():
    return BeatGrid.from_tempo(120, 0.0, 30.0, 4)


def test_set_bar_one_rephases_and_numbers():
    g = set_bar_one(_grid(), 4.6)                       # beat at 4.5 is the nearest
    assert g.confirmed and g.bar_one == 4.5
    assert 4.5 in g.downbeats and 6.5 in g.downbeats and 2.5 in g.downbeats
    k = g.downbeats.index(4.5)
    assert g.bar_number(k) == 1 and g.bar_number(k - 1) == 0 and g.bar_number(k + 2) == 3
    g2 = move_bar_one(g, 1)
    assert g2.bar_one == 5.0 and 5.0 in g2.downbeats
    g3 = drop_beats_before_bar_one(g2)
    assert g3.beats[0] == 5.0 and g3.bar_number(0) == 1
    # survives halving and save/load
    assert halve_tempo(g).bar_one == g.bar_one
    p = Project()
    p.beat_grid = g
    assert Project.from_dict(p.to_dict()).beat_grid.bar_one == 4.5


def test_bar_one_defaults_to_first_downbeat():
    g = BeatGrid([0.5, 1.0, 1.5, 2.0, 2.5], [0.5, 2.5], 4)
    assert g.bar_one_index() == 0 and g.bar_number(1) == 2


def _mono(audio):
    import librosa
    return librosa.resample(audio.mean(1).astype(np.float32), orig_sr=SR, target_sr=22050)


def test_leading_gap_gets_no_beats():
    audio, info = make_song(seed=2)
    y = _mono(audio)
    gap = 3.3
    y = np.r_[np.zeros(int(gap * 22050), np.float32), y]
    start, _ = music_bounds(y, 22050)
    assert gap - 0.1 < start < gap + 0.6
    g = detect_beats_librosa(y, 22050)
    assert g.beats[0] > gap - 0.3                       # nothing invented in the silence
    assert g.bar_one is not None and g.bar_one >= gap - 0.3


def test_downbeats_survive_a_free_pause():
    audio, info = make_song(seed=3)
    y = _mono(audio)
    db = np.asarray(info["downbeats"])
    cut = db[len(db) // 2 + 2]
    pause = 1.37                                        # not a whole number of beats
    i = int(cut * 22050)
    y = np.r_[y[:i], np.zeros(int(pause * 22050), np.float32), y[i:]]
    beat = 60 / 120
    assert any(a < cut + 0.1 and b > cut + pause - 0.1 for a, b in internal_gaps(y, 22050, beat))
    g = detect_beats_librosa(y, 22050, beats_per_bar=4)
    est = np.asarray(g.downbeats)
    after = db[db > cut + 0.5] + pause
    before = db[(db < cut - 0.5) & (db > 30)]
    for ref in (after[:6], before[-6:]):
        hit = [np.min(np.abs(est - r)) < 0.08 for r in ref]
        assert sum(hit) >= len(ref) - 1, (ref, est)


# ------------------------------------------------------------------ CuePoints CSV
CP = ("Track\tType\tPosition\tCue No\tLabel\tFade\n"
      "RunBoyRun\tLighting\t07:00:00:00\t36\tBong\t0\n"
      "RunBoyRun\tLighting\t07:00:03:15\t36.1\tBong 2\t0\n"
      "RunBoyRun\tStrobe\t7:0:10:5\t\tHit\t\n"
      "Other\tLighting\t08:00:01:00\t1\tGo\t2\n"
      "Other\tLighting\tnope\t1\tGo\t2\n")


def test_cuepoints_parse_and_import():
    t = read_table(CP)
    m = guess_mapping(t)
    assert m["song"] == 0 and m["lane"] == 1 and m["time"] == 2 and m["number"] == 3 and m["label"] == 4
    p = Project()
    p.frame_rate_key = "25"
    items, problems = rows(t, ImportOptions(mapping=m), p.frame_rate)
    assert len(items) == 4 and len(problems) == 1
    r = import_rows(p, items, ImportOptions(mapping=m))
    assert r["cues"] == 4 and r["songs_created"] == ["RunBoyRun", "Other"]
    rb = next(s for s in p.songs if s.name == "RunBoyRun")
    assert rb.tc_offset == 7 * 3600
    times = sorted(round(c.time, 3) for c in rb.cues)
    assert times == [0.0, 3.6, 10.2]
    assert {c.number for c in rb.cues} == {36.0, 36.1, None}
    # importing again doesn't duplicate
    assert import_rows(p, items, ImportOptions(mapping=m))["cues"] == 0


def test_cuepoints_comma_headerless_and_times():
    t = read_table("0:12.5,Verse\n75,Chorus\n")
    assert t.headers == ["Column 1", "Column 2"] and len(t.rows) == 2
    assert parse_time("1:02.5", None) == 62.5 and parse_time("75", None) == 75.0
    assert parse_time("", None) is None and parse_time("abc", None) is None


# ------------------------------------------------------------------ bulk
def test_song_groups_from_folder(tmp_path):
    (tmp_path / "Song C" / "Stems").mkdir(parents=True)
    for f in ("A.wav", "B.mp3", "notes.txt", "Song C/mix.wav", "Song C/click.wav", "Song C/Stems/drums.wav"):
        (tmp_path / f).write_bytes(b"")
    g = song_groups_from_folder(str(tmp_path))
    names = [n for n, _ in g]
    assert names == ["A", "B", "Song C"]
    assert [os.path.basename(x) for x in g[2][1]] == ["click.wav", "mix.wav", "drums.wav"]
    assert song_groups_from_files(["/x/b.wav", "/x/a.wav"]) == [("a", ["/x/a.wav"]), ("b", ["/x/b.wav"])]


# ------------------------------------------------------------------ progress model
def test_progress_model_is_smooth_and_learns():
    m = ProgressModel(4.0, deep=True, demucs=False, skip={0.55})
    reports = [(0, 0.0), (2, 0.02), (4, 0.08), (80, 0.25), (88, 0.35), (98, 0.45), (104, 0.6), (115, 0.8)]
    fr, last, j = [], 0.0, 0
    for t in np.arange(0, 120, 0.15):
        while j < len(reports) and reports[j][0] <= t:
            m.feed(reports[j][1], reports[j][0])
            j += 1
        f = m.fraction(t)
        fr.append(f)
        assert f >= last
        last = f
    assert max(np.diff(fr)) < 0.06                      # no big jumps (UI polls every 0.15 s)
    learned = m.learn()
    assert learned["0.08d"] < 25.0                      # beats took 76 s for 4 min: 19 s/min


# ------------------------------------------------------------------ AI add-ons
def test_addons_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("CUEFORGE_AI_DIR", str(tmp_path / "pk"))
    monkeypatch.setenv("CUEFORGE_AI_PRIVATE", "1")
    args = addons.pip_args(["beat_this"], gpu=True)
    assert "--target" in args and str(tmp_path / "pk") in args and "--only-binary=:all:" in args
    assert "torch" in args and "beat_this>=1.0" in args and not any(a.startswith("demucs") for a in args)
    if sys.platform != "darwin":
        assert addons.GPU_INDEX in args
    prog, a, env = addons.install_command(["demucs"], str(tmp_path / "l.log"))
    assert a[:2] == ["-m", "cueforge.addons"] and "--pip" in a and "PYTHONPATH" in env
    assert addons.handle_cli(["cueforge"]) is None
    assert addons.handle_cli(["cueforge", "project.cueproj"]) is None


def test_addons_pip_mode_installs_into_private_folder(monkeypatch, tmp_path):
    """The in-app pip path: run pip in-process into a target folder (offline: a local wheel)."""
    pytest.importorskip("pip")
    import zipfile
    whl = tmp_path / "cfdummy-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("cfdummy/__init__.py", "VALUE = 42\n")
        z.writestr("cfdummy-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: cfdummy\nVersion: 1.0\n")
        z.writestr("cfdummy-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                   "Tag: py3-none-any\n")
        z.writestr("cfdummy-1.0.dist-info/RECORD", "")
    target = tmp_path / "pk"
    log = tmp_path / "pip.log"
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = subprocess.call([sys.executable, "-m", "cueforge.addons", "--pip", str(log), "install", "--no-index",
                            "--disable-pip-version-check", "--target", str(target), str(whl)],
                           env={**os.environ, "PYTHONPATH": root})
    assert code == 0, log.read_text()
    assert (target / "cfdummy" / "__init__.py").exists()
    assert "[pip finished with code 0]" in log.read_text()


def test_review_regressions(tmp_path):
    from cueforge.core.timecode import FRAME_RATES
    assert parse_time("00:01:02.500", FRAME_RATES["25"]) == 62.5
    assert parse_time("1:02:03.25", FRAME_RATES["25"]) == 3723.25
    assert read_table(",,,\n,,").rows == []
    # rows without a Track go to the song that was open, even after a named song
    p = Project()
    p.add_song("Open one")
    p.select_song(p.songs[0].id)
    t = read_table("Track,Type,Position\nNewSong,Lighting,01:00:01:00\n,Lighting,00:00:05:00\n")
    items, _ = rows(t, ImportOptions(mapping=guess_mapping(t)), p.frame_rate)
    import_rows(p, items, ImportOptions(mapping=guess_mapping(t)))
    assert [round(c.time, 2) for c in p.songs[0].cues] == [5.0]
    # new lanes get a free MA3 sequence
    seqs = [l.ma3_sequence for l in p.lanes]
    assert len(seqs) == len(set(seqs))
    # halving keeps bar 1 on a beat (3/4, bar 1 on an odd beat)
    g = set_bar_one(BeatGrid.from_tempo(120, 0, 20, 3), 1.5)
    h = halve_tempo(g)
    assert h.bar_one == 1.5 and h.downbeats[h.bar_one_index()] == 1.5
    # a song folder with only Stems/ and Click/ sub-folders is one song
    for f in ("Song/Stems/drums.wav", "Song/Stems/bass.wav", "Song/Click/click.wav"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_bytes(b"")
    assert [n for n, _ in song_groups_from_folder(str(tmp_path))] == ["Song"]


def _bar_lengths(g):
    b = np.asarray(g.beats)
    idx = np.searchsorted(b, np.asarray(g.downbeats))
    return np.diff(idx)


def test_stop_time_keeps_the_grid():
    """Stabs with rests (the band keeps counting) must not restart bars or bend the grid."""
    audio, info = make_song(seed=4)
    y = _mono(audio).copy()
    db = np.asarray(info["downbeats"])
    for k in range(10, 14):                            # four bars: hit on the one, then rest
        a, b = int((db[k] + 0.25) * 22050), int(db[k + 1] * 22050)
        y[a:b] *= 0.002
    g = detect_beats_librosa(y, 22050, beats_per_bar=4)
    assert (_bar_lengths(g) == 4).all(), _bar_lengths(g)
    est = np.asarray(g.downbeats)
    hits = [np.min(np.abs(est - d)) < 0.07 for d in db[db > 18]]
    assert np.mean(hits) > 0.9


def test_regular_bars_follow_real_odd_bars_only():
    from cueforge.analysis.beats import regular_bars
    beats = np.arange(0, 40, 0.5)
    downs = list(np.arange(0, 8, 2.0)) + [8.0] + list(np.arange(9.0, 40, 2.0))     # one real 2/4 bar
    assert list(beats[regular_bars(beats, np.array(downs), 4)][:7]) == [0, 2, 4, 6, 8, 9, 11]
    stray = [x for x in np.arange(0, 40, 2.0) if x != 10.0] + [13.5]                # one missing, one stray
    assert list(beats[regular_bars(beats, np.array(stray), 4)]) == list(np.arange(0, 40, 2.0))


def test_model_grid_is_tidied():
    """Raw deep-model output (jitter, missed / doubled beats, stray downbeats) becomes a
    clean grid with regular bars."""
    from cueforge.analysis.beats import _tidy_model_grid
    audio, info = make_song(seed=5)
    y = _mono(audio)
    rng = np.random.default_rng(1)
    beats = np.asarray(info["beats"])
    b = beats + rng.normal(0, 0.012, len(beats))
    b = b[rng.random(len(b)) > 0.04]
    b = np.sort(np.r_[b, [(b[i] + b[i + 1]) / 2 for i in range(5, len(b) - 1, 23)]])
    d = np.asarray(info["downbeats"])
    d = np.sort(np.r_[d[rng.random(len(d)) > 0.1], beats[7::37]])
    g = _tidy_model_grid(BeatGrid(list(b), list(d), 4, "Beat This!"), y, 22050)
    assert (_bar_lengths(g) == 4).all()
    est = np.asarray(g.downbeats)
    assert np.mean([np.min(np.abs(est - x)) < 0.07 for x in info["downbeats"]]) > 0.9
