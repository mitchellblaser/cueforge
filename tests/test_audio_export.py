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
from cueforge.core.model import Project, lane_per_song
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
    e.take_meters()                                   # meters hold their peak until read
    assert e.render(0, 100)[50, 0] == pytest.approx(0.2)
    m = e.take_meters()
    assert m["a"] == 0.0 and m["b"] == pytest.approx(0.2)
    assert e.take_meters()["b"] == 0.0


def test_blips_do_not_stack_and_meter():
    e = _engine_with([("a", 0.0, 48000)])
    e.set_blips([0.0005], True, -10)
    one = float(abs(e.render(0, 2000)).max())
    e.set_blips([0.0005, 0.0005, 0.001, 0.0005], True, -10)  # four lanes with a cue at once
    four = float(abs(e.render(0, 2000)).max())
    assert four == pytest.approx(one, rel=0.05) and one > 0
    assert e.take_meters()["__blips__"] == pytest.approx(four, rel=0.05)
    e.set_click([0.0005], [0.0005], True, -6)
    e.render(0, 2000)
    assert e.take_meters()["__click__"] > 0


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
    from cueforge.core.editing import sequence_plan
    pl = sequence_plan(p)                      # shared Hits first, then the song's Main Cues, from 1
    assert pl == {"Hits": 1, "Song 1 Main Cues": 2}
    assert tracks[1].get("Target").endswith("Sequences.1") and tracks[0].get("Target").endswith("Sequences.2")
    p.export.ma3_seq_start = 40
    assert build_ma3_xml(p).count('Target="ShowData.DataPools.Default.Sequences.4') == 2   # 40 and 41
    p.export.ma3_seq_start = 1
    evs = tracks[0].findall(".//CmdEvent")
    assert [e.get("CueDestination") for e in evs] == ["Intro", 'Verse "1"']
    assert float(evs[1].get("Time")) == 16.0                   # seconds, as MA3 writes them
    rc = evs[1].find("RealtimeCmd")
    seq = int(tracks[0].get("Target").rsplit(".", 1)[1])
    assert rc.get("Object") == f"12.12.0.5.{seq - 1}" and rc.get("ValCueDestination").endswith(".2000")
    assert "Offset" not in xml and "Guid" not in xml and root.find("Timecode").get("TimeDisplayFormat") == "Default"


def test_ma3_lua_and_macro():
    p = _project_with_cues()
    lua = build_ma3_lua(p, 3, all_songs=False)
    assert "tc=3" in lua and 'seq="Hits", pref=1' in lua and 'label="Verse \\"1\\""' in lua
    assert 'seq="Song 1 Main Cues"' in lua and 'offset="0h00m00.000"' in lua
    assert "CF_ENSURE(lane.seq, lane.pref" in lua and "cf_set_offset(song.tc, song.offset)" in lua
    assert lua.count("{t=") == 3
    cmds = build_ma3_macro_commands(p)
    assert 'Store Sequence "Song 1 Main Cues" Cue 1 /Merge /NoConfirm' in cmds    # found by name
    assert 'Label Sequence "Song 1 Main Cues" Cue 2 "Verse \'1\'"' in cmds


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
    assert n == 3 and rows[1][5] == "01:00:01:00" and rows[2][2] == "Hits"


def test_learn_thresholds():
    rng = np.random.default_rng(0)
    hist = []
    for _ in range(100):
        c = float(rng.uniform(0.3, 1.0))
        hist.append({"kind": "hit", "confidence": c, "accepted": c > 0.8})
    res = learn_thresholds(hist)
    assert 0.75 <= res["hit"]["threshold"] <= 0.82
    assert "section" not in res


def test_ma3_temp_exports_temp_on_off(tmp_path):
    p = _project_with_cues()
    c = p.cues_in_lane(p.lanes[0].id)[0]      # at 1.0 s
    c.duration = 0.5                            # a Temp with 0.5 s hold
    root = ET.fromstring(build_ma3_xml(p))
    cmds = [(float(e.get("Time")), e.find("RealtimeCmd").get("ExecToken"), e.find("RealtimeCmd").get("Status"))
            for e in root.findall(".//Track")[0].findall(".//CmdEvent")]
    assert cmds[0] == (1.0, "Temp", "On")
    assert cmds[1] == (1.5, "Temp", "Off")
    assert cmds[2] == (16.0, "Goto", "On")         # the normal cue stays a Goto
    lua = build_ma3_lua(p)
    assert "off=1.500000" in lua and 'e.token = "Temp"' in lua
    _luac_ok(lua, tmp_path)


def _luac_ok(src, tmp_path):
    import shutil
    import subprocess
    luac = shutil.which("luac") or shutil.which("luac5.4")
    if not luac:
        pytest.skip("luac not installed")
    f = tmp_path / "plugin.lua"
    f.write_text(src)
    r = subprocess.run([luac, "-p", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_setlist_exports(tmp_path):
    from cueforge.export.csv_export import export_csv
    from cueforge.export.ma3 import cue_number_clashes, export_ma3_xml_all
    p = _project_with_cues()
    s2 = p.add_song("Second Song")
    assert s2.tc_offset == pytest.approx(3600) and s2.ma3_timecode == 2 and s2.cue_start == 101
    p.select_song(s2.id)
    main, hits = p.lanes[0], next(l for l in p.lanes if l.name == "Hits")
    assert lane_per_song(main) and not lane_per_song(hits)
    editing.add_cue(p, main.id, 5.0, label="Song2 intro")
    editing.add_cue(p, main.id, 9.0)
    editing.add_cue(p, hits.id, 6.0)
    # Main Cues: the song's own sequence, found by name "<song> <lane>", numbered from 1
    assert sorted(editing.effective_cue_numbers(p, main.id).values()) == [1, 2]
    assert editing.sequence_name(p, main) == "Second Song Main Cues"
    # Hits: one sequence shared by every song (named after the lane), this song's cues from 101
    assert list(editing.effective_cue_numbers(p, hits.id).values()) == [101]
    assert editing.sequence_name(p, hits) == "Hits"
    assert not cue_number_clashes(p)
    paths = export_ma3_xml_all(p, str(tmp_path / "xml"))
    assert len(paths) == 2 and "Second Song" in paths[1]
    root = ET.parse(paths[1]).getroot()
    assert root.find("Timecode").get("Name") == "Second Song"
    first = min(float(e.get("Time")) for e in root.iter("CmdEvent"))
    assert first == 5.0                          # song time; the song's 01:00:00:00 is its Offset TC Slot
    lua = build_ma3_lua(p, all_songs=True)
    assert lua.count("{name=") >= 2 and "tc=2" in lua and "cue=101" in lua
    _luac_ok(lua, tmp_path)
    cmds = build_ma3_macro_commands(p, all_songs=True)
    assert 'Store Sequence "Second Song Main Cues" Cue 1 /Merge /NoConfirm' in cmds   # song 2's own
    assert 'Store Sequence "Song 1 Main Cues" Cue 1 /Merge /NoConfirm' in cmds        # song 1's
    assert 'Store Sequence "Hits" Cue 101 /Merge /NoConfirm' in cmds                  # shared Hits
    # current song unchanged by the exports
    assert p.song.id == s2.id
    # clash in the shared Hits sequence when song 2's range overlaps song 1's
    s2.cue_start = 1
    assert cue_number_clashes(p)
    s2.cue_start = 101
    assert not cue_number_clashes(p)
    # two songs with the same name would share their per-song sequences
    s2.name = "Song 1"
    assert cue_number_clashes(p)
    s2.name = "Second Song"
    path = tmp_path / "all.csv"
    export_csv(p, str(path), all_songs=True)
    rows = list(csv.reader(open(path, encoding="utf-8")))
    assert rows[0][0] == "Song" and {r[0] for r in rows[1:]} == {"Song 1", "Second Song"}


def test_lua_single_song_compiles(tmp_path):
    _luac_ok(build_ma3_lua(_project_with_cues()), tmp_path)


def test_go_plus_tokens_and_warnings():
    from cueforge.export.ma3 import cue_tokens, go_plus_warnings
    p = _project_with_cues()
    lane = p.lanes[0].id
    editing.add_cue(p, lane, 20.0, label="Chorus")
    toks = [cue_tokens(p, lane)[c.id] for c in p.cues_in_lane(lane)]
    assert toks == ["Goto", "Go+", "Go+"]          # first cue resyncs, then Go+
    root = ET.fromstring(build_ma3_xml(p))
    assert [e.find("RealtimeCmd").get("ExecToken") for e in root.findall(".//Track")[0].findall(".//CmdEvent")] \
        == ["Goto", "Go+", "Go+"]
    p.export.ma3_first_goto = False
    assert set(cue_tokens(p, lane).values()) == {"Go+"}
    p.export.ma3_cue_token = "Goto"
    assert set(cue_tokens(p, lane).values()) == {"Goto"}
    p.export.ma3_cue_token = "Go+"
    assert not go_plus_warnings(p)
    p.cues_in_lane(lane)[0].number = 10             # first cue numbered after the others
    p.cues_in_lane(lane)[-1].number = 2
    assert any("not in time order" in w for w in go_plus_warnings(p))
    p.cues_in_lane(lane)[0].number = None
    p.cues_in_lane(lane)[-1].number = None
    p.cues_in_lane(lane)[1].duration = 0.5          # Temp mixed with Go+ cues -> warning
    assert any("Temps" in w for w in go_plus_warnings(p))


def test_plugin_imports_every_song(tmp_path):
    from cueforge.export.ma3 import export_ma3_lua
    p = _project_with_cues()
    s2 = p.add_song("Encore")
    p.select_song(s2.id)
    editing.add_cue(p, p.lanes[0].id, 3.0, label="Encore go")
    lua = build_ma3_lua(p)                          # whole setlist by default
    assert lua.count("xml=[==[") == 2 and lua.count("<GMA3") == 2
    assert "Import Timecode" in lua and 'name="Encore"' in lua and "tc=2" in lua
    assert 'tok="Go+"' in lua or 'tok="Goto"' in lua
    _luac_ok(lua, tmp_path)
    desc = export_ma3_lua(p, str(tmp_path / "Show.lua"))
    assert (tmp_path / "Show.lua").exists() and desc.endswith("Show.xml")


def test_scrub_grains():
    e = _engine_with([("a", 0.0, 48000 * 4)])
    ramp = np.linspace(0, 1, 48000 * 4, dtype=np.float32)
    e.set_track("a", np.stack([ramp, ramp], 1))
    g = e.scrub_grain(1.0, 1.0)
    assert len(g) == int(e.GRAIN * 48000)
    mid = len(g) // 2
    assert g[mid, 0] == pytest.approx(ramp[48000 + mid], abs=1e-3)       # forward slice at t=1s
    r = e.scrub_grain(2.0, -1.0)
    assert r[mid, 0] > r[mid + 100, 0]                                   # reversed when dragging back
    # stopped engine outputs the queued grain, then silence
    e._scrub_buf = g
    out = e._next_block(len(g) + 100)
    assert np.allclose(out[:len(g)], g) and not out[len(g):].any()


def test_ma3_roundtrip_import():
    from cueforge.core.model import Project
    from cueforge.export.ma3_import import import_into_song, parse_timecode_xml
    p = _project_with_cues()
    p.tc_offset = 7200
    lane0 = p.lanes[0].id
    temp = editing.add_cue(p, lane0, 30.0, label="Strobe hit")
    temp.duration = 0.5
    p.cues_in_lane(lane0)[1].number = 7
    for unit in ("ticks", "seconds"):
        p.export.ma3_time_unit = unit
        show = parse_timecode_xml(build_ma3_xml(p))
        assert show.offset is None                 # events at song time; the offset lives on the console
        q = Project()
        q.lanes[1].ma3_sequence = 7               # lane that matches the Hits track's sequence
        info = import_into_song(q, show)
        assert info["cues"] == len(p.cues)
        got = sorted((round(c.time, 3), c.label, c.duration, q.lane(c.lane_id).ma3_sequence) for c in q.cues)
        want = sorted((round(c.time, 3), c.label, c.duration, p.lane(c.lane_id).ma3_sequence) for c in p.cues)
        assert got == want
        assert q.tc_offset == 0                     # no offset in the file: the song's start is left alone
        assert any(c.number == 7 for c in q.cues)
    # merging again doesn't duplicate; replace swaps the lane's cues
    assert import_into_song(q, show, replace=False)["cues"] == 0


def test_ma3_import_tolerates_console_layout():
    from cueforge.core.model import Project
    from cueforge.export.ma3_import import import_into_song, parse_timecode_xml
    xml = """<?xml version="1.0"?><GMA3 DataVersion="2.0"><Timecode Name="From console">
      <TrackGroup><Track Name="Front wash" Target="ShowData.DataPools.Default.Sequences.Seq 12">
        <TimeRange><CmdSubTrack>
          <CmdEvent Name="Look 1" Time="33554432"><RealtimeCmd Token="Go+" Status="On"/></CmdEvent>
          <CmdEvent Time="50331648"><RealtimeCmd Token="Temp" Status="On"/></CmdEvent>
          <CmdEvent Time="58720256"><RealtimeCmd Token="Temp" Status="Off"/></CmdEvent>
        </CmdSubTrack></TimeRange></Track></TrackGroup></Timecode></GMA3>"""
    q = Project()
    info = import_into_song(q, parse_timecode_xml(xml))
    assert info["lanes_created"] == ["Front wash"]
    lane = next(l for l in q.lanes if l.name == "Front wash")
    assert lane.ma3_sequence == 12
    cues = q.cues_in_lane(lane.id)
    assert [(c.time, c.label, c.duration) for c in cues] == [(2.0, "Look 1", None), (3.0, "", 0.5)]


def test_playhead_clock_is_smoothed(monkeypatch):
    """A late audio block must not make the playhead jump; a real jump (loop) resets it."""
    import time as _t
    import types
    from cueforge.audio import engine as engine_mod
    now = [1000.0]                                        # a fake clock: no dependence on CI speed
    monkeypatch.setattr(engine_mod, "time", types.SimpleNamespace(perf_counter=lambda: now[0], sleep=_t.sleep,
                                                                   monotonic=lambda: now[0]))
    e = _engine_with([("a", 0.0, 48000 * 30)])
    e._playing = True
    e._set_clock(5.0, 0.05)
    p0 = e.position()
    now[0] += 0.05
    e._set_clock(5.0 + 0.05 + 0.02, 0.05)                 # 20 ms off: eased, not jumped
    assert abs(e.position() - (p0 + 0.05)) < 0.005
    e._set_clock(1.0, 0.05)                               # loop back: reset
    assert abs(e.position() - (1.0 - 0.05)) < 1e-9
    e._playing = False


def test_ma3_xml_round_trip_in_ma_layout():
    from cueforge.export.ma3_import import parse_timecode_xml, show_to_cues
    p = _project_with_cues()
    p.song.tc_offset = 3600.0
    c = p.cues_in_lane(p.lanes[0].id)[0]
    c.duration = 0.5
    xml = build_ma3_xml(p)
    show = parse_timecode_xml(xml)
    assert show.offset is None                     # song time; Offset TC Slot is set on the console
    assert min(float(e.get("Time")) for e in ET.fromstring(xml).iter("CmdEvent")) == 1.0
    # a timecode exported from the console carries its offset
    show = parse_timecode_xml(xml.replace("<Timecode ", '<Timecode OffsetTCSlot="1h00m00.000" ', 1))
    assert show.offset == 3600.0
    got = show_to_cues(show)
    times = sorted(cue.time for _, _, cue in got)
    assert times[0] == 1.0 and any(cue.duration == 0.5 for _, _, cue in got)
    assert all(cue.number is not None for _, _, cue in got)


def _decoded_seconds(sig, rate, sr=48000):
    from cueforge.export.ltc import decode_ltc
    from cueforge.core.timecode import tc_to_frame_count
    fr = decode_ltc(sig, sr, rate)
    assert fr
    out = []
    for i, h, m, s, f in fr:
        out.append((i, tc_to_frame_count(h, m, s, f, rate) / rate.fps))
    return out


def test_live_ltc_striped_on_the_playback_device():
    """Mono mix on channel 1, LTC on channel 2, sample-locked to the song position."""
    from cueforge.core.timecode import get_rate
    e = _engine_with([("a", 0.25, 48000 * 30)])
    e.ltc.rate, e.ltc.offset = get_rate("25"), 3600.0
    e.mix_channels, e.ltc_mode, e.ltc_channel = (0,), "same", 1
    assert e.out_channels() == 2
    e.seek(10.0)
    e._playing = True
    blocks = []
    for _ in range(40):
        mix, ltc = e._next_blocks(1024, 0.0, with_ltc=True)
        blocks.append(e.route(mix, ltc, e.out_channels()))
    e._playing = False
    out = np.concatenate(blocks)
    assert np.allclose(out[:, 0], 0.25)                          # mono mix on the left only
    for i, t in _decoded_seconds(out[:, 1], e.ltc.rate):
        assert abs(t - (3600.0 + 10.0 + i / 48000)) < 1.5 / 48000 * 25   # frame starts on its sample
    # stopped: the timecode channel goes silent so the console holds
    mix, ltc = e._next_blocks(512, 0.0, with_ltc=True)
    assert not ltc.any()


def test_live_ltc_on_a_second_device_follows_the_clock(monkeypatch):
    import types
    from cueforge.audio import engine as engine_mod
    from cueforge.core.timecode import get_rate
    now = [50.0]
    monkeypatch.setattr(engine_mod, "time", types.SimpleNamespace(perf_counter=lambda: now[0]))
    e = _engine_with([("a", 0.0, 48000 * 60)])
    e.ltc.rate, e.ltc.offset = get_rate("30"), 0.0
    e._playing = True
    e._set_clock(20.0, 0.0)                     # song time 20 s is heard now
    sig = []
    for _ in range(30):
        sig.append(e.ltc_block(256, 0.01))      # blocks heard 10 ms from now
        now[0] += 256 / 48000
        e._set_clock(e.position(), 0.0)
    times = _decoded_seconds(np.concatenate(sig), e.ltc.rate)
    for i, t in times:
        assert abs(t - (20.01 + i / 48000)) < 0.002
    e._set_clock(5.0, 0.0)                      # a seek: followed at once
    t0 = e._ltc_t
    e.ltc_block(256, 0.0)
    assert abs(e._ltc_t - (5.0 + 256 / 48000)) < 1e-6 and t0 > 20


def test_route_stereo_pair_and_mono():
    e = AudioEngine()
    mix = np.column_stack([np.full(4, 0.2, np.float32), np.full(4, 0.4, np.float32)])
    e.mix_channels = (2, 3)
    out = e.route(mix, None, 4)
    assert np.allclose(out[:, 2], 0.2) and np.allclose(out[:, 3], 0.4) and not out[:, :2].any()
    e.mix_channels = (0,)
    assert np.allclose(e.route(mix, None, 2)[:, 0], 0.3)
