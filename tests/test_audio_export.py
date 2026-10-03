import csv
import os
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cueforge.analysis.tuning import learn_thresholds
from cueforge.audio.engine import AudioEngine, db_to_gain
from cueforge.audio.loader import ENGINE_SR, PeakPyramid, load_audio
from cueforge.core import editing
from cueforge.core.model import Project
from cueforge.core.timecode import FRAME_RATES, frame_count_to_tc
from cueforge.export.csv_export import export_csv
from cueforge.export.ltc import decode_ltc, generate_ltc, render_ltc_for_project
from cueforge.export.ma3 import MA3_TICKS_PER_SECOND, build_ma3_lua, build_ma3_xml, build_ma3_macro_commands


def test_load_resample_mono(tmp_path):
    sr = 44100
    y = np.sin(2 * np.pi * 440 * np.arange(sr) / sr).astype(np.float32) * 0.5
    path = tmp_path / "mono.wav"
    sf.write(path, y, sr)
    a = load_audio(str(path))
    assert a.sr == ENGINE_SR and a.samples.shape[1] == 2
    assert a.duration == pytest.approx(1.0, abs=1e-3)
    assert a.original_channels == 1


def test_peaks_envelope():
    sr = 48000
    y = np.zeros(sr * 2, np.float32)
    y[sr:sr + 100] = 0.9
    pk = PeakPyramid.build(y, sr)
    mn, mx = pk.envelope(0, 2, 200)
    assert mx[:90].max() == 0
    assert mx[100] == pytest.approx(0.9)
    mn, mx = pk.envelope(0.9, 1.1, 2000)  # zoomed in
    assert mx.max() == pytest.approx(0.9)


def _engine_with(tracks):
    e = AudioEngine()
    for tid, val, n in tracks:
        e.set_track(tid, np.full((n, 2), val, np.float32))
    return e


def test_mixer_gain_mute_solo():
    e = _engine_with([("a", 0.1, 48000), ("b", 0.2, 48000)])
    out = e.render(0, 100)
    assert out[50, 0] == pytest.approx(0.3)
    e.update_track("a", gain_db=-6)
    assert e.render(0, 100)[50, 0] == pytest.approx(0.1 * db_to_gain(-6) + 0.2, abs=1e-5)
    e.update_track("b", mute=True)
    assert e.render(0, 100)[50, 0] == pytest.approx(0.1 * db_to_gain(-6), abs=1e-5)
    e.update_track("b", mute=False, solo=True)
    assert e.render(0, 100)[50, 0] == pytest.approx(0.2)
    assert e.meters["a"] == 0.0 and e.meters["b"] == pytest.approx(0.2)


def test_track_offset_and_varispeed():
    e = _engine_with([("a", 0.5, 48000)])
    e.update_track("a", offset=1.0)
    assert e.render(0.0, 100)[50, 0] == 0
    assert e.render(1.0, 100)[50, 0] == pytest.approx(0.5)
    assert e.duration == pytest.approx(2.0)
    out = e.render(1.0, 100, speed=0.5)
    assert out[50, 0] == pytest.approx(0.5)


def test_click_and_blips():
    e = AudioEngine()
    e.set_click([0.5, 1.0], [0.5], True, 0)
    out = e.render(0, 48000 * 2)
    nz = np.where(np.abs(out[:, 0]) > 1e-3)[0]
    assert nz[0] == pytest.approx(24000, abs=50)
    e.set_click([], [], False, 0)
    e.set_blips([0.25], True, 0)
    out = e.render(0.2, 48000)
    nz = np.where(np.abs(out[:, 0]) > 1e-3)[0]
    assert nz[0] == pytest.approx(0.05 * 48000, abs=50)
    # a click that started before the block still sounds (tail carried over)
    e.set_blips([0.0], True, 0)
    out = e.render(0.01, 480)
    assert np.abs(out).max() > 1e-3


def test_engine_loop_and_transport():
    e = _engine_with([("a", 0.1, 48000 * 10)])
    e.loop = (1.0, 1.5)
    e.loop_enabled = True
    e._playing = True
    e._pos = 1.4
    e._next_block(48000 // 5)
    assert 1.0 <= e._pos < 1.5


@pytest.mark.parametrize("key", ["24", "25", "29.97df", "30"])
def test_ltc_roundtrip(key):
    rate = FRAME_RATES[key]
    start = 3600 * rate.nominal + 1790  # crosses a minute boundary for df
    sig = generate_ltc(start, 60, rate, 48000, -6)
    frames = decode_ltc(sig, 48000, rate)
    assert len(frames) >= 58
    expected = [frame_count_to_tc(start + i, rate) for i in range(60)]
    got = [f[1:] for f in frames]
    for g in got:
        assert g in expected
    # frame start positions line up with the real frame rate
    first = frames[1]
    idx = expected.index(first[1:])
    assert first[0] == pytest.approx(idx * 48000 / rate.fps, abs=3)


def test_render_ltc_for_project_alignment():
    rate = FRAME_RATES["30"]
    sig, t0 = render_ltc_for_project(0.0, 2.0, 3600.0, rate)
    assert t0 == pytest.approx(0.0)
    frames = decode_ltc(sig, 48000, rate)
    by_tc = {f[1:]: f[0] for f in frames}
    assert by_tc[(1, 0, 0, 2)] == pytest.approx(2 * 1600, abs=3)


def _project_with_cues():
    p = Project()
    p.name = "Song"
    p.frame_rate_key = "30"
    l0, l1 = p.lanes[0], p.lanes[1]
    a = editing.add_cue(p, l0.id, 1.0, label="Intro")
    b = editing.add_cue(p, l0.id, 16.0, label='Verse "1"')
    editing.add_cue(p, l1.id, 2.0)
    l1.ma3_sequence = 7
    return p


def test_ma3_xml():
    p = _project_with_cues()
    xml = build_ma3_xml(p)
    root = ET.fromstring(xml)
    assert root.tag == "GMA3"
    tracks = root.findall(".//Track")
    assert len(tracks) == 2
    assert tracks[1].get("Target").endswith("Sequences.7")
    evs = tracks[0].findall(".//CmdEvent")
    assert [e.get("Name") for e in evs] == ["Intro", 'Verse "1"']
    assert int(evs[1].get("Time")) == 16 * MA3_TICKS_PER_SECOND
    assert evs[1].find("RealtimeCmd").get("Cue").endswith("Cues.2")
    p.export.ma3_time_unit = "seconds"
    root = ET.fromstring(build_ma3_xml(p))
    assert root.findall(".//CmdEvent")[1].get("Time") == "16"


def test_ma3_lua_and_macro():
    p = _project_with_cues()
    lua = build_ma3_lua(p, 3)
    assert "local TC_NUMBER = 3" in lua and "seq=7" in lua and 'label="Verse \\"1\\""' in lua
    assert lua.count("{t=") == 3
    cmds = build_ma3_macro_commands(p)
    assert "Store Sequence 1 Cue 1 /Merge /NoConfirm" in cmds
    assert 'Label Sequence 1 Cue 2 "Verse \'1\'"' in cmds


def test_ma3_lua_files(tmp_path):
    from cueforge.export.ma3 import export_ma3_lua
    p = _project_with_cues()
    xml_path = export_ma3_lua(p, str(tmp_path / "Song.lua"))
    root = ET.parse(xml_path).getroot()
    assert root.find("Plugin/ComponentLua").get("FileName") == "Song.lua"
    assert (tmp_path / "Song.lua").exists()


def test_csv(tmp_path):
    p = _project_with_cues()
    p.tc_offset = 3600
    path = tmp_path / "cues.csv"
    n = export_csv(p, str(path))
    rows = list(csv.reader(open(path, encoding="utf-8")))
    assert n == 3 and rows[1][4] == "01:00:01:00" and rows[2][1] == "7"


def test_learn_thresholds():
    rng = np.random.default_rng(0)
    hist = []
    for _ in range(100):
        c = float(rng.uniform(0.3, 1.0))
        hist.append({"kind": "hit", "confidence": c, "accepted": c > 0.8})
    res = learn_thresholds(hist)
    assert 0.75 <= res["hit"]["threshold"] <= 0.82
    assert "section" not in res


def test_ma3_hold_exports_off_event():
    p = _project_with_cues()
    c = p.cues_in_lane(p.lanes[0].id)[0]
    c.duration = 2.0
    root = ET.fromstring(build_ma3_xml(p))
    evs = root.findall(".//Track")[0].findall(".//CmdEvent")
    offs = [e for e in evs if e.find("RealtimeCmd").get("Token") == "Off"]
    assert len(offs) == 1 and int(offs[0].get("Time")) == 3 * MA3_TICKS_PER_SECOND
    assert "off=3.000000" in build_ma3_lua(p)
