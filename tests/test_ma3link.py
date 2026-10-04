"""grandMA3 live link: preview commands, cue-list sync, timecode push (over real UDP)."""
import os
import socket
import sys
import xml.etree.ElementTree as ET

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from cueforge.control.ma3link import MA3Link
from cueforge.core.editing import effective_cue_numbers
from cueforge.core.model import Cue
from cueforge.core.settings import UserSettings
from cueforge.ui.session import Session


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


class FakeEngine:
    def __init__(self):
        self.playing = False
        self.pos = 0.0

    def position(self):
        return self.pos


@pytest.fixture()
def link(app, tmp_path):
    s = Session(UserSettings(str(tmp_path / "settings.json")))
    real = s.engine
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(1)
    lk = MA3Link(s)
    lk.cfg.enabled = True
    lk.cfg.port = rx.getsockname()[1]
    lk.cfg.timecode_dir = str(tmp_path / "timecodes")
    lk.apply()
    lk._tick_timer.stop()           # ticks are driven by hand
    lk._sync_timer.stop()
    lk.rx = rx
    p = s.project
    main, hits = p.lanes[0], p.lanes[1]
    p.cues += [Cue(lane_id=main.id, time=1.0, label="Intro"), Cue(lane_id=main.id, time=5.0, label="Verse"),
               Cue(lane_id=hits.id, time=3.0, duration=0.5)]
    p.sort_cues()
    s.engine = FakeEngine()
    yield lk
    s.engine = real
    real.close()
    rx.close()


def plan(project):
    from cueforge.core.editing import sequence_plan
    return sequence_plan(project)


def q(project, lane, song=None):
    """A sequence as commands address it: its quoted name."""
    from cueforge.core.editing import sequence_name
    return f'"{sequence_name(project, lane, song)}"'


def received(rx):
    from pythonosc.osc_message import OscMessage
    out = []
    while True:
        try:
            data, _ = rx.recvfrom(4096)
        except socket.timeout:
            return out
        m = OscMessage(data)
        out.append((m.address, m.params[0]))
        rx.settimeout(0.3)


def test_preview_fires_cues_and_releases_temps(link):
    s = link.s
    seq_main = q(s.project, s.project.lanes[0])
    seq_hits = q(s.project, s.project.lanes[1])
    eng = s.engine
    eng.playing, eng.pos = True, 0.5
    link.tick()                                    # start: resync (nothing before 0.5 s)
    assert link.log == []
    for t in (0.9, 1.1, 1.5, 1.9, 2.3, 2.7, 3.01, 3.3, 3.6, 4.0, 4.4, 4.8, 5.2):
        eng.pos = t
        link.tick()
    assert link.log == [f"Goto Sequence {seq_main} Cue 1",          # first cue of a lane is a Goto
                        f"Temp Sequence {seq_hits} Cue 1",
                        f"Off Sequence {seq_hits}",
                        f"Go+ Sequence {seq_main}"]
    msgs = received(link.rx)
    assert msgs[0] == ("/cmd", f"Goto Sequence {seq_main} Cue 1")
    assert len(msgs) == 4


def test_jump_resyncs_and_stop_releases(link):
    s = link.s
    eng = s.engine
    seq_main = q(s.project, s.project.lanes[0])
    seq_hits = q(s.project, s.project.lanes[1])
    eng.playing, eng.pos = True, 3.2                # start inside the Temp
    link.tick()
    assert link.log == [f"Goto Sequence {seq_main} Cue 1", f"Temp Sequence {seq_hits} Cue 1"]
    eng.playing = False
    link.tick()                                     # stop: the held Temp is released
    assert link.log[-1] == f"Off Sequence {seq_hits}"
    link.log.clear()
    eng.playing, eng.pos = True, 6.0
    link.tick()
    eng.pos = 6.02
    link.tick()
    eng.pos = 1.5                                   # jump back (loop / click on ruler)
    link.tick()
    assert link.log == [f"Goto Sequence {seq_main} Cue 2", f"Goto Sequence {seq_main} Cue 1"]


def test_sync_creates_labels_pins_and_deletes(link):
    s = link.s
    p = s.project
    main = p.lanes[0]
    seq = q(p, main)
    cmds = link.push_all()
    assert f"Store Sequence {seq} Cue 1 /Merge /NoConfirm" in cmds
    assert f'Label Sequence {seq} Cue 1 "Intro"' in cmds
    assert f'Label Sequence {seq} Cue 2 "Verse"' in cmds
    ensure = f'Lua "CF_ENSURE([[{seq.strip(chr(34))}]],{plan(p)[seq.strip(chr(34))]},1)"'
    assert ensure in cmds                                   # created (and named) on the console by Lua
    assert cmds.index(ensure) < cmds.index(f"Store Sequence {seq} Cue 1 /Merge /NoConfirm")
    assert any("CF_P[1]=" in c for c in cmds[:cmds.index(ensure)])      # CF_ENSURE itself sent first
    # numbers are now fixed: a cue inserted in between gets 1.1 and nothing shifts
    # (except Temps in a shared lane: that lane's one Temp cue is fixed by rule)
    from cueforge.core.model import lane_per_song
    assert all(c.number is not None for c in p.cues if lane_per_song(p.lane(c.lane_id)) or not c.duration)
    assert all(c.number is None for c in p.cues if not lane_per_song(p.lane(c.lane_id)) and c.duration)
    p.cues.append(Cue(lane_id=main.id, time=3.0, label='Big "drop"'))
    p.sort_cues()
    assert sorted(effective_cue_numbers(p, main.id).values()) == [1, 1.1, 2]
    cmds = link.sync_cues()
    assert cmds == [f"Store Sequence {seq} Cue 1.1 /Merge /NoConfirm", f"Label Sequence {seq} Cue 1.1 \"Big 'drop'\""]
    # relabel → only a Label
    p.cues_in_lane(main.id)[0].label = "Opening"
    assert link.sync_cues() == [f'Label Sequence {seq} Cue 1 "Opening"']
    # delete: nothing is sent unless allowed
    p.cues = [c for c in p.cues if c.label != "Verse"]
    assert link.sync_cues() == []
    link.cfg.allow_delete = True
    assert link.sync_cues() == [f"Delete Sequence {seq} Cue 2 /NoConfirm"]
    assert link.sync_cues() == []


def test_song_switch_and_reload_do_not_restore(link, tmp_path):
    """Review #1: switching songs or reopening the project must not re-Store every cue."""
    s = link.s
    p = s.project
    link.push_all()
    p.add_song("Second")
    for k in (1, 0):                                   # what Session.switch_song does
        p.select_song(p.songs[k].id)
        s.project_replaced.emit()
    # with two songs, per-song sequences are renamed "<song> <lane>" once; nothing is stored again
    assert not any(c.startswith("Store") for c in link.sync_cues())
    assert link.sync_cues() == []
    from cueforge.core.model import Project
    s.project = Project.from_dict(p.to_dict())        # saved + reopened
    assert link.sync_cues() == []


def test_scope_changes_never_delete(link):
    """Review #2: leaving the sync scope (all songs off, lane not exported) is not a delete."""
    s = link.s
    p = s.project
    link.cfg.allow_delete = True
    link.cfg.all_songs = True
    p.add_song("Second")
    song2 = p.songs[-1]
    p.select_song(song2.id)
    p.cues.append(Cue(lane_id=p.lanes[0].id, time=2.0, label="S2"))
    p.select_song(p.songs[0].id)
    seq2 = q(p, p.lanes[0], song2)                                   # song 2's own Main Cues
    assert any(f"Sequence {seq2} Cue 1 " in c for c in link.sync_cues())
    link.cfg.all_songs = False
    p.lanes[1].export = False
    assert link.sync_cues() == []


def test_undo_keeps_pinned_numbers(link):
    """Review #4: undo restoring unnumbered cues must not shift console cue numbers."""
    p = link.s.project
    main = p.lanes[0]
    seq = q(p, main)
    p.cues.append(Cue(lane_id=main.id, time=0.5, label="X"))
    p.sort_cues()
    link.push_all()
    snap = [(c.id, c.number) for c in p.cues]
    for c in p.cues:                                   # what an undo snapshot would restore
        c.number = None
    p.cues = [c for c in p.cues if c.label != "X"]
    assert not any(c.startswith("Label") and "Cue 1 " in c for c in link.sync_cues())
    assert {c.label: c.number for c in p.cues_in_lane(main.id)} == {"Intro": 2.0, "Verse": 3.0}
    assert snap


def test_apply_and_preview_off_release_temps(link):
    """Review #7: changing settings while a Temp holds releases it."""
    s = link.s
    seq_hits = q(s.project, s.project.lanes[1])
    s.engine.playing, s.engine.pos = True, 3.2
    link.tick()
    link.cfg.preview = False
    link.tick()
    assert link.log[-1] == f"Off Sequence {seq_hits}"
    link.cfg.preview = True
    s.engine.playing = False
    link.tick()
    s.engine.playing = True
    link.tick()
    n = len(link.log)
    link.apply()
    assert link.log[n:] == [f"Off Sequence {seq_hits}"]


def test_custom_templates(link):
    link.cfg.cmd_go = "Go Sequence {seq}"
    link.cfg.cmd_temp_off = "Off Sequence {seq}"
    link.cfg.cmd_goto = "broken {nope}"            # bad template falls back to the default
    assert link.cmd("go", 3) == "Go Sequence 3"
    assert link.cmd("temp_off", 3) == "Off Sequence 3"
    assert link.cmd("goto", 3, 4.5) == "Goto Sequence 3 Cue 4.5"


def test_timecode_push_goes_over_the_network(link, tmp_path):
    p = link.s.project
    slot = p.song.ma3_timecode
    link.cfg.timecode_dir = str(tmp_path / "nowhere")        # old setting: ignored, no files here
    cmds = link.push_timecode()
    assert len(cmds) >= 2 and all(c.startswith('Lua "') and c.endswith('"') for c in cmds)
    assert all(c.count('"') == 2 and "@" not in c and "%" not in c for c in cmds)   # command-line safe
    assert "load(CF_D(h))" in cmds[-1] and len(cmds[-1]) <= 200
    assert not (tmp_path / "nowhere").exists() and not (tmp_path / "timecodes").exists()
    assert all(len(c) <= 200 for c in cmds)                   # MA cuts long command lines off
    msgs = received(link.rx)
    assert [m[1] for m in msgs][-len(cmds):] == cmds
    link.cfg.max_cmd = 150                                    # adjustable for a stricter console
    short = link.push_timecode()
    assert max(len(c) for c in short[1:-2]) <= 150 and len(short) > len(cmds)


def test_all_songs_sync(link):
    s = link.s
    p = s.project
    p.add_song("Second")
    song2 = p.songs[-1]
    p.select_song(song2.id)
    p.cues.append(Cue(lane_id=p.lanes[0].id, time=2.0, label="S2"))
    hits = p.lanes[1]
    p.cues.append(Cue(lane_id=hits.id, time=3.0, duration=0.5))
    link.cfg.all_songs = True
    cmds = link.push_all()
    stores = {c for c in cmds if c.startswith("Store")}
    main = p.lanes[0]
    seq1, seq2 = q(p, main, p.songs[0]), q(p, main, song2)
    assert f"Store Sequence {seq2} Cue 1 /Merge /NoConfirm" in stores    # song 2's own Main Cues, from 1
    assert f"Store Sequence {seq1} Cue 1 /Merge /NoConfirm" in stores    # song 1's
    # Hits is shared: song 2's Temp fires the one shared Temp cue of that sequence
    assert 'Store Sequence "Hits" Cue 1 /Merge /NoConfirm' in stores
    assert not any(f'Sequence "Hits" Cue {song2.cue_start:g} ' in c for c in stores)
    pl = plan(p)                         # shared lanes first, then each song's own, from Sequence start
    assert f'Lua "CF_ENSURE([[Second {main.name}]],{pl["Second " + main.name]},1)"' in cmds
    assert f'Lua "CF_ENSURE([[{hits.name}]],{pl[hits.name]},1)"' in cmds
    assert pl[hits.name] < pl[f"Song 1 {main.name}"] < pl[f"Second {main.name}"]


def test_disabled_link_sends_nothing(link):
    link.cfg.enabled = False
    link.apply()
    assert not link.active
    assert link.sync_cues() == []
    link.s.engine.playing = True
    link.tick()
    assert link.log == []


def test_point_numbers_never_collide():
    """Review #3: many cues squeezed between fixed numbers stay unique and in order."""
    from cueforge.core.model import Project
    p = Project()
    lane = p.lanes[0].id
    p.cues[:] = [Cue(lane_id=lane, time=0, number=5)] + [Cue(lane_id=lane, time=i + 1) for i in range(30)] + \
        [Cue(lane_id=lane, time=40, number=6)]
    nums = [effective_cue_numbers(p, lane)[c.id] for c in p.cues_in_lane(lane)]
    assert nums == sorted(nums) and len(set(nums)) == len(nums) and nums[-1] == 6


def test_import_keeps_tiny_times():
    """Review #6: a cue at one frame (671089 ticks) is not read as seconds."""
    from cueforge.core.model import Project
    from cueforge.export.ma3 import build_ma3_xml
    from cueforge.export.ma3_import import parse_timecode_xml, show_to_cues
    p = Project()
    p.cues.append(Cue(lane_id=p.lanes[0].id, time=0.04))
    p.cues.append(Cue(lane_id=p.lanes[0].id, time=12.0))
    assert [round(c.time, 3) for _, _, c in show_to_cues(parse_timecode_xml(build_ma3_xml(p)))] == [0.04, 12.0]


def test_temps_share_one_cue(link):
    from cueforge.export.ma3 import build_ma3_macro_commands, build_ma3_xml
    s = link.s
    p = s.project
    strobe = p.lanes[1]
    p.cues += [Cue(lane_id=strobe.id, time=t, duration=0.25, label="Strobe") for t in (6.0, 9.0, 12.0)]
    p.cues.append(Cue(lane_id=strobe.id, time=7.0, number=7.0, duration=0.3))   # an old per-Temp number
    p.sort_cues()
    nums = effective_cue_numbers(p, strobe.id)
    temps = [c for c in p.cues_in_lane(strobe.id)]
    assert len({nums[c.id] for c in temps}) == 1                                 # one Temp cue
    cmds = link.sync_cues()
    stores = [c for c in cmds if c.startswith(f'Store Sequence "{strobe.name}" ')]
    assert len(stores) == 1
    assert sum(1 for c in cmds if c.startswith(f'Label Sequence "{strobe.name}" Cue')) == 1
    macro = [c for c in build_ma3_macro_commands(p) if c.startswith(f'Store Sequence "{strobe.name}" ')]
    assert len(macro) == 1
    # the timecode track fires Temp On / Off on that one cue every time
    xml = build_ma3_xml(p)
    root = ET.fromstring(xml)
    temps_ev = [e for e in root.iter("RealtimeCmd") if e.get("ExecToken") == "Temp"]
    assert len(temps_ev) == 2 * len(temps) and len({e.get("ValCueDestination") for e in temps_ev}) == 1
    # editing a Temp's number moves the shared cue
    s._sync_engine = lambda: None                                               # no audio here
    s.update_cue(temps[-1].id, number=20.0)
    assert set(effective_cue_numbers(p, strobe.id).values()) == {20.0}


def test_link_test_command():
    from cueforge.ui.link_dialog import TEST_CMD
    assert TEST_CMD.startswith("Lua ") and "Printf" in TEST_CMD


def test_old_default_address_moves_to_cmd_once(app, tmp_path):
    s = Session(UserSettings(str(tmp_path / "s.json")))
    s.settings.set("ma3link", {"address": "/gma3/cmd"})
    lk = MA3Link(s)
    assert lk.cfg.address == "/cmd"
    lk.cfg.address = "/gma3/cmd"                    # chosen on purpose afterwards: kept
    lk.save()
    assert MA3Link(s).cfg.address == "/gma3/cmd"
    s.engine.close()


def test_timecode_xml_has_no_guids(link):
    from cueforge.export.ma3 import build_ma3_xml
    xml = build_ma3_xml(link.s.project)
    assert "Guid" not in xml                     # MA3 rejected ours ("Illegal property"); it makes its own
    ET.fromstring(xml)


def _console_run(tmp_path, cmds):
    """Play `Lua "…"` commands into a Lua interpreter that mocks the console: its disk is
    tmp_path/console; sequence 1 'Song 1 Main Cues', sequence 5 'Hits' with cues 1 and 2.5
    (stored × 1000, as MA does); timecodes 3 and 4 record their Offset TC Slot."""
    import shutil
    import subprocess
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    if not lua:
        pytest.skip("no Lua interpreter")
    disk = tmp_path / "console"
    (disk / "datapools" / "timecodes").mkdir(parents=True)
    mock = ("local function mk(no, addr, kids, name) return {no=no, name=name, AddrNative=function() return addr end, "
            "Children=function() return kids end} end "
            "local cues = {mk(0, 'S5.0'), mk(1000, 'S5.1'), mk(2500, 'S5.2')} "
            "local list = {mk(1, 'S1', {}, 'Song 1 Main Cues'), mk(5, 'S5', cues, 'Hits')} "
            "local function tcode(no) local t = {no=no} "
            "function t:Set(k, v) self[k] = v print('SET ' .. k .. ' ' .. v) end "
            "function t:Get(k) return self[k] end return t end "
            "local tcs = {tcode(3), tcode(4)} "
            "function DataPool() return {Sequences={Children=function() return list end}, "
            "Timecodes={Children=function() return tcs end}} end "
            "Enums = {PathType={Library='lib'}, Roles={Display=1}} "
            f"function GetPath(k) if k == 'lib' then return [[{disk}]] end error('no') end "
            "function ErrPrintf(f, ...) print('ERR ' .. string.format(f, ...)) end "
            "function Printf(f, ...) print(string.format(f, ...)) end "
            "function Cmd(c) print('CMD ' .. c) end\n")
    # each command runs on its own, like separate command-line entries
    body = "".join(";(function() " + c[len('Lua "'):-1] + " end)()\n" for c in cmds)
    runner = tmp_path / "run.lua"
    runner.write_text(mock + body)
    out = subprocess.run([lua, str(runner)], check=True, capture_output=True, text=True).stdout
    return out, disk / "datapools" / "timecodes"


def test_network_push_writes_on_console_and_imports(tmp_path):
    from cueforge.control.ma3link import import_script, push_commands
    from cueforge.export.ma3 import SEQ_FIX_LUA
    xml = ('<t Target="@TGT2@"/><a Object="@SEQ2@" V="@CUE2:2.5@" q="it\'s &quot;x&quot;"/><b V="@CUE2:9@"/>'
           '<c V="@SEQ1@"/>' + "<pad/>" * 400)
    cmds = push_commands(import_script(xml, 3, SEQ_FIX_LUA, ["Song 1 Main Cues", "Hits"], "1h00m00.000"))
    assert len(cmds) > 5                                       # several pieces
    out, tc = _console_run(tmp_path, cmds)
    got = (tc / "CueForge_3.xml").read_text()
    # sequences found by name, addresses from the console
    assert got.startswith('<t Target="ShowData.DataPools.Default.Sequences.5"/><a Object="S5" V="S5.2" '
                          'q="it\'s &quot;x&quot;"/><b V=""/><c V="S1"/>')
    assert "Hits cue 9 not found" in out
    assert out.index("CMD Import Timecode 3") > out.index("ERR")         # import after the fix
    assert "Timecode 3 imported" in out
    assert out.index("SET OffsetTCSlot 1h00m00.000") > out.index("CMD Import Timecode 3")   # offset on the show
    # a lost packet is caught instead of importing a broken file
    for lost in (2, len(cmds) - 3):                                      # one in the middle, the last piece
        out, tc = _console_run(tmp_path / f"lost{lost}", cmds[:lost] + cmds[lost + 1:])
        assert "lost data, push again" in out and "CMD Import" not in out


def test_network_push_of_a_real_song_arrives_intact(link, tmp_path):
    """The packed XML expands on the console to exactly what CueForge built."""
    from cueforge.control.ma3link import import_script, push_commands
    from cueforge.export.ma3 import SEQ_FIX_LUA, build_ma3_xml, seq_table
    p = link.s.project
    for k in range(40):
        p.cues.append(Cue(lane_id=p.lanes[k % 2].id, time=10 + k * 0.75, label=f'Hit "{k}"',
                          duration=0.25 if k % 3 == 0 else None))
    p.sort_cues()
    xml = build_ma3_xml(p, placeholders=True)
    cmds = push_commands(import_script(xml, 4, SEQ_FIX_LUA, [n for n, _ in seq_table(p)]))
    out, tc = _console_run(tmp_path, cmds)
    got = (tc / "CueForge_4.xml").read_text()
    import re
    flat = "\n".join(line.lstrip("\t") for line in xml.split("\n"))          # indentation is dropped
    no_addr = lambda s: re.sub(r'(Target|Object|ValCueDestination)="[^"]*"', "", s)   # addresses get filled in
    assert no_addr(got) == no_addr(flat) and "@SEQ" not in got and "@TGT" not in got
    assert 'Target="ShowData.DataPools.Default.Sequences.1"' in got     # 'Song 1 Main Cues' is seq 1 here
    ET.fromstring(got)                                          # still valid XML


def test_ensure_creates_missing_sequences_by_name(tmp_path):
    """CF_ENSURE finds a sequence by name, or creates it in the first free number from the
    lane's seq (no 100-gaps) with its first cue, and names it."""
    from cueforge.control.ma3link import push_commands
    from cueforge.export.ma3 import ENSURE_LUA
    cmds = push_commands(ENSURE_LUA) + ['Lua "CF_ENSURE([[Hits]],5,1)"',              # exists: nothing
                                        'Lua "CF_ENSURE([[Second Main Cues]],1,1)"']  # 1 taken: 2
    out, _ = _console_run(tmp_path, cmds)
    assert "CMD Store Sequence 2 Cue 1 /Merge /NoConfirm" in out
    assert 'CMD Label Sequence 2 "Second Main Cues"' in out
    assert out.count("CMD Store") == 1


def test_fades_sync_to_console(link):
    from cueforge.export.ma3 import build_ma3_macro_commands
    p = link.s.project
    main = p.lanes[0]
    intro = p.cues_in_lane(main.id)[0]
    intro.fade = 2.5
    seq = q(p, main)
    cmds = link.sync_cues()
    assert f"Sequence {seq} Cue 1 CueFade 2.5" in cmds
    assert not any("CueFade" in c and "Cue 2 " in c for c in cmds)       # no fade: left alone
    assert not any("CueFade" in c for c in link.sync_cues())              # nothing changed
    intro.fade = 1.0
    assert link.sync_cues() == [f"Sequence {seq} Cue 1 CueFade 1"]
    intro.fade = None
    assert link.sync_cues() == [f"Sequence {seq} Cue 1 CueFade 0"]       # removed: back to 0
    intro.fade = 3.0
    assert f"Sequence {seq} Cue 1 CueFade 3" in build_ma3_macro_commands(p)


def test_old_project_moves_to_per_song_sequences(link):
    """A two-song show pushed before per-song sequences: song 2's Main Cues (pinned 101, 102
    in Sequence 1) move to their own sequence, numbered from 1; hand-typed numbers stay."""
    from cueforge.core.model import Project
    s = link.s
    p = s.project
    s2 = p.add_song("Second")
    s2.seq_offset = 0                                  # as projects were before
    main = p.lanes[0]
    p.select_song(s2.id)
    p.cues += [Cue(lane_id=main.id, time=1.0), Cue(lane_id=main.id, time=2.0),
               Cue(lane_id=main.id, time=3.0, number=150.0)]          # last one typed by hand
    p.sort_cues()
    link.sync_cues()                                   # pins 101, 102 (old behaviour: shared range)
    d = p.to_dict()
    d.pop("per_song_seqs")
    for sd in d["songs"]:
        sd["seq_offset"] = 0
    for c in d["songs"][1]["cues"]:                    # what the old numbering had pinned
        if c["number"] is None or c["number"] < 150:
            c["number"] = {1.0: 101.0, 2.0: 102.0}[c["time"]]
            d["console"]["cues"][c["id"]] = [main.ma3_sequence, c["number"], ""]
    q = Project.from_dict(d)
    q.select_song(q.songs[1].id)
    assert q.songs[1].seq_offset == 100
    assert sorted(effective_cue_numbers(q, main.id).values()) == [1, 2, 150]


def test_shared_lane_temps_fire_one_cue_in_every_song(link):
    from cueforge.core.model import Project
    from cueforge.export.ma3 import cue_number_clashes
    s = link.s
    s._sync_engine = lambda: None
    p = s.project
    strobe = next(l for l in p.lanes if l.name == "Strobe")
    p.cues += [Cue(lane_id=strobe.id, time=t, duration=0.3) for t in (5.0, 9.0)]
    s2 = p.add_song("Second")
    p.select_song(s2.id)
    p.cues += [Cue(lane_id=strobe.id, time=t, duration=0.3) for t in (2.0, 4.0)]
    p.cues.append(Cue(lane_id=strobe.id, time=6.0))                     # a normal cue skips the Temp cue
    nums2 = effective_cue_numbers(p, strobe.id)
    assert sorted(set(nums2.values())) == [1.0, 101.0]                   # Temps: 1; normal cue: 101
    p.select_song(p.songs[0].id)
    assert set(effective_cue_numbers(p, strobe.id).values()) == {1.0}
    assert not cue_number_clashes(p)                                     # the shared Temp cue is fine
    # typing a number on any Temp moves the one cue for the whole setlist
    s.update_cue(p.cues_in_lane(strobe.id)[0].id, number=50.0)
    p.select_song(s2.id)
    assert {n for cid, n in effective_cue_numbers(p, strobe.id).items()
            if p.cue(cid).duration} == {50.0}
    # an older project: song 2's Temps pinned at the old per-song number come back to the shared cue
    # (old scheme: the normal cue was 101, the Temp cue the next whole number, 102)
    for c in s2.cues:
        if c.lane_id == strobe.id:
            c.number = 102.0 if c.duration else 101.0
            p.console.setdefault("cues", {})[c.id] = [strobe.ma3_sequence, c.number, ""]
    for c in p.songs[0].cues:
        if c.lane_id == strobe.id:
            c.number = None
    d = p.to_dict()
    d.pop("shared_temps2")
    q = Project.from_dict(d)
    q.select_song(q.songs[1].id)
    assert {n for cid, n in effective_cue_numbers(q, strobe.id).items() if q.cue(cid).duration} == {1.0}


def test_sender_runs_in_the_background_and_live_goes_first():
    import threading
    from cueforge.control.ma3link import OscSender
    order, gate = [], threading.Event()

    class Client:
        def send_message(self, addr, cmd):
            if cmd == "bulk0":
                gate.wait(2)                       # the first bulk send stalls the network
            order.append(cmd)
    sender = OscSender()
    c = Client()
    import time as _t
    t0 = _t.perf_counter()
    for k in range(5):
        sender.put(c, "/cmd", f"bulk{k}", bulk=True)
    assert _t.perf_counter() - t0 < 0.05            # queuing never waits on the network
    sender.put(c, "/cmd", "live", bulk=False)
    gate.set()
    assert sender.wait_idle(3)
    assert order.index("live") < order.index("bulk2")                  # live jumps the queue
    assert [x for x in order if x.startswith("bulk")] == [f"bulk{k}" for k in range(5)]   # bulk keeps order


def test_auto_timecode_push_waits_for_stop_and_skips_unchanged(link):
    link.cfg.push_timecode = True
    link.s.engine.playing = True
    link._maybe_push_timecode()
    assert not any(c.startswith('Lua "CF_P') for c in link.log)          # nothing while playing
    link.s.engine.playing = False
    link._maybe_push_timecode()
    n = len(link.log)
    assert n and any(c.startswith('Lua "CF_P') for c in link.log)
    link._maybe_push_timecode()
    assert len(link.log) == n                                            # unchanged: not pushed again
    assert link.push_timecode()                                          # the button always pushes


def test_new_shared_lane_used_only_in_song_two(link):
    """'Strobe Plate Hit' (a user lane, shared by its name), not used in song 1: song 2's
    Temps fire the setlist's shared cue 1 — also after an older version fixed them at 101 —
    and the link never fixes a shared lane's Temp number again."""
    from cueforge.core.model import Lane, Project, lane_per_song
    s = link.s
    p = s.project
    lane = Lane("Strobe Plate Hit", ma3_sequence=9)
    p.lanes.append(lane)
    assert not lane_per_song(lane)
    s2 = p.add_song("Second")
    p.select_song(s2.id)
    p.cues += [Cue(lane_id=lane.id, time=t, duration=0.2) for t in (3.0, 7.0)]
    assert set(effective_cue_numbers(p, lane.id).values()) == {1.0}
    link.sync_cues()
    assert all(c.number is None for c in p.cues_in_lane(lane.id))       # not fixed by the link
    # what an older version left behind: both Temps fixed at 101, no matching console record
    for c in p.cues_in_lane(lane.id):
        c.number = 101.0
    p.console = {}
    d = p.to_dict()
    d.pop("shared_temps2")
    q = Project.from_dict(d)
    q.select_song(q.songs[1].id)
    assert set(effective_cue_numbers(q, lane.id).values()) == {1.0}


def test_renaming_a_song_renames_its_sequences(link):
    p = link.s.project
    main = p.lanes[0]
    link.sync_cues()
    p.song.name = "Opener"
    cmds = link.sync_cues()
    assert f'Label Sequence "Song 1 {main.name}" "Opener {main.name}"' in cmds   # renamed, not re-created
    assert not any(c.startswith("Store") for c in cmds)
    assert not any("CF_ENSURE([[Opener" in c for c in cmds)
    assert link.sync_cues() == []


def test_old_number_records_adopt_sequences_once(link):
    """A show synced when sequences were numbers: song 1's Main Cues (Seq 1) is renamed to
    'Song 1 Main Cues' and kept; song 2's cues that sat in the same Seq 1 (101…) are not
    adopted, so song 2 gets its own sequence."""
    s = link.s
    p = s.project
    main = p.lanes[0]
    s2 = p.add_song("Second")
    p.select_song(s2.id)
    p.cues.append(Cue(lane_id=main.id, time=2.0, label="S2"))
    p.select_song(p.songs[0].id)
    link.cfg.all_songs = True
    rec = link.record
    for song, base in ((p.songs[0], 1), (s2, 101)):
        for k, c in enumerate(sorted((c for c in song.cues if c.lane_id == main.id), key=lambda c: c.time)):
            rec[c.id] = [main.ma3_sequence, float(base + k), c.label or ""]
    cmds = link.sync_cues()
    assert f'Label Sequence {main.ma3_sequence} "Song 1 {main.name}"' in cmds          # adopted once
    assert sum(c.startswith(f"Label Sequence {main.ma3_sequence} ") for c in cmds) == 1
    assert f'Lua "CF_ENSURE([[Second {main.name}]],{plan(p)["Second " + main.name]},1)"' in cmds   # own one
    assert f'Store Sequence "Second {main.name}" Cue 1 /Merge /NoConfirm' in cmds
    assert not any(f'Store Sequence "Song 1 {main.name}"' in c for c in cmds)           # already there


# ---------------------------------------------------------------- console -> CueForge
MA_EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<GMA3 DataVersion="2.1.1.5">
  <Timecode Name="Song 1" Guid="AA" OffsetTCSlot="1h00m00.000">
    <TrackGroup>
      <Track Name="Song 1 Main Cues" Guid="BB" Target="ShowData.DataPools.Default.Sequences.1">
        <TimeRange Duration="To End">
          <CmdSubTrack>
            <CmdEvent Name="Goto" Time="1.250" CueDestination="Intro, &quot;big&quot;">
              <RealtimeCmd Type="Key" Status="On" Object="12.12.0.5.0" ExecToken="Goto" ValCueDestination="12.12.0.5.0.1000"/>
            </CmdEvent>
            <CmdEvent Name="Go+" Time="9.000" CueDestination="New one">
              <RealtimeCmd Type="Key" Status="On" Object="12.12.0.5.0" ExecToken="Go+" ValCueDestination="12.12.0.5.0.3000"/>
            </CmdEvent>
          </CmdSubTrack>
        </TimeRange>
      </Track>
      <Track Name="Hits" Guid="CC" Target="ShowData.DataPools.Default.Sequences.2">
        <TimeRange Duration="To End">
          <CmdSubTrack>
            <CmdEvent Name="Temp" Time="3.000" CueDestination="Hits">
              <RealtimeCmd Type="Key" Status="On" ExecToken="Temp" ValCueDestination="12.12.0.5.1.1000"/>
            </CmdEvent>
            <CmdEvent Name="Temp" Time="3.750" CueDestination="Hits">
              <RealtimeCmd Type="Key" Status="Off" ExecToken="Temp" ValCueDestination="12.12.0.5.1.1000"/>
            </CmdEvent>
          </CmdSubTrack>
        </TimeRange>
      </Track>
    </TrackGroup>
  </Timecode>
</GMA3>
"""


def _console_pull(tmp_path, export_xml, force=True, runs=1):
    """Run CF_PULL on the mock console (its Export writes `export_xml`); returns the text
    CueForge would assemble from the SendOSC replies, one per run."""
    import shutil
    import subprocess
    from cueforge.control.ma3link import CONSOLE_DIR_LUA, push_commands
    from cueforge.control.ma3pull import PULL_LUA
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    if not lua:
        pytest.skip("no Lua interpreter")
    disk = tmp_path / "console"
    (disk / "datapools" / "timecodes").mkdir(parents=True, exist_ok=True)
    src = tmp_path / "export.xml"
    src.write_text(export_xml)
    mock = ("local function tcode(no) local t = {no=no} "
            f"function t:Export(d, f) local i = io.open([[{src}]]) local x = i:read('*a') i:close() "
            "local o = io.open(d .. '/' .. f, 'w') o:write(x) o:close() return true end return t end "
            "local tcs = {tcode(1)} "
            "local function q(no, name) return {no=no, name=name} end "
            "local main = {no=7, name='Song 1 Main Cues', Children=function() return "
            "{q(0, 'OffCue'), q(1000, 'Intro'), q(1500, 'Big, \"new\"'), q(2000, 'Verse')} end} "
            "function DataPool() return {Sequences={Children=function() return {main} end}, "
            "Timecodes={Children=function() return tcs end}} end "
            "CF_SEQS = {'Song 1 Main Cues', 'Not there'} "
            "Enums = {PathType={Library='lib'}} "
            f"function GetPath(k) if k == 'lib' then return [[{disk}]] end error('no') end "
            "function ErrPrintf(f, ...) print('ERR ' .. string.format(f, ...)) end "
            "function Printf(f, ...) print(string.format(f, ...)) end "
            "function Cmd(c) print('CMD ' .. c) end\n")
    cmds = push_commands(CONSOLE_DIR_LUA + PULL_LUA)
    cmds += [f'Lua "CF_PULL(2,{"true" if force else "false"},1)"'] * runs
    body = "".join(";(function() " + c[len('Lua "'):-1] + " end)()\n" for c in cmds)
    (tmp_path / "pull.lua").write_text(mock + body)
    out = subprocess.run([lua, str(tmp_path / "pull.lua")], check=True, capture_output=True, text=True).stdout
    sends = [l for l in out.splitlines() if l.startswith("CMD SendOSC")]
    assert all(len(l) - 4 <= 200 for l in sends)                    # short commands
    assert all(l.startswith('CMD SendOSC 2 "/cueforge/tc,s,') and l.count('"') == 2 and l.count(",") == 1 + 1
               for l in sends)                                       # one string argument, no stray commas
    # reassemble like PullReceiver
    texts, parts = [], {}
    import re
    for l in sends:
        slot, i, n, payload = re.fullmatch(r'CMD SendOSC 2 "/cueforge/tc,s,(\d+):(\d+):(\d+):(.*)"', l).groups()
        parts[int(i)] = payload
        if len(parts) == int(n):
            texts.append("".join(parts[k] for k in range(1, int(n) + 1)))
            parts = {}
    return texts


def test_console_pull_round_trip(tmp_path):
    from cueforge.control.ma3pull import parse_pull
    from cueforge.export.ma3_import import show_to_cues
    texts = _console_pull(tmp_path, MA_EXPORT)
    show = parse_pull(texts[0])
    items = show_to_cues(show)
    got = sorted((t, c.time, c.duration, c.number, c.label) for _, t, c in items)
    assert got == [("Hits", 3.0, 0.75, 1.0, "Hits"),
                   ("Song 1 Main Cues", 1.25, None, 1.0, 'Intro, "big"'),          # commas and quotes survive
                   ("Song 1 Main Cues", 9.0, None, 3.0, "New one")]
    # unchanged on the next look: the console only says "same"
    texts = _console_pull(tmp_path / "again", MA_EXPORT, force=False, runs=2)
    assert texts[1] == "same"


def test_merge_keeps_labels_fades_and_notes(link):
    from cueforge.control.ma3pull import merge_into_song, parse_pull
    from cueforge.export.ma3_import import TcEvent, TcShow, TcTrack, show_to_cues
    p = link.s.project
    main = p.lanes[0]
    intro, verse = p.cues_in_lane(main.id)[:2]               # cues 1 (1.0 s) and 2 (5.0 s)
    intro.fade, intro.notes = 2.0, "keep me"
    show = TcShow("Song 1", None, [TcTrack("Song 1 Main Cues", None, [
        TcEvent(1.25, "Goto", "On", 1.0, "Intro"),            # cue 1 moved
        TcEvent(9.0, "Go+", "On", 3.0, "New one")])])         # cue 3 added; cue 2 deleted on the console
    stats = merge_into_song(p, show_to_cues(show))
    assert stats["moved"] == 1 and stats["added"] == 1 and stats["removed"] == 1
    c1 = p.cue(intro.id)
    assert c1.time == 1.25 and c1.fade == 2.0 and c1.notes == "keep me" and c1.label == "Intro"
    assert p.cue(verse.id) is None
    assert any(c.label == "New one" and c.number == 3.0 for c in p.cues_in_lane(main.id))


def test_auto_pull_takes_console_edits_only_when_cueforge_is_unchanged(link):
    """First look: remember the console's version. Console edited since: bring it in (one
    undo step). Edited on both sides: keep CueForge's and say so."""
    from cueforge.control.ma3pull import own_items
    s = link.s
    s._sync_engine = lambda: None
    p = s.project
    main = p.lanes[0]
    intro = p.cues_in_lane(main.id)[0]
    msgs = []
    link.status.connect(msgs.append)

    def console_text(items):
        lines = []
        for tname in sorted({t for _, t, _ in items}):
            lines.append(f"T|{tname}|")
            for _, t, c in items:
                if t != tname:
                    continue
                h = f"12.12.0.5.0.{int(round((c.number or 1) * 1000))}"
                lines.append(f"E|{c.time:.3f}|{'Temp' if c.duration else 'Goto'}|On|{h}|{c.label}")
                if c.duration:
                    lines.append(f"E|{c.time + c.duration:.3f}|Temp|Off|{h}|")
        return "%0A".join(lines)
    slot = p.song.ma3_timecode
    same = console_text(own_items(p))
    link._on_pulled(slot, same)                                     # in sync: nothing happens
    assert p.cue(intro.id).time == 1.0
    link._on_pulled(slot, same.replace("E|1.000|", "E|1.500|", 1))   # cue 1 moved on the console
    assert p.cue(intro.id).time == 1.5 and msgs[-1].startswith("From the console: 1 moved")
    s.undo.undo()
    p = s.project
    assert p.cue(intro.id).time == 1.0                               # one undo step
    # edited on both sides: CueForge's version stays
    p.cue(intro.id).time = 1.5
    link._on_pulled(slot, console_text(own_items(p)))               # console == CueForge again
    link._pushed_items[p.song.id] = own_items(p)                    # what was pushed: 1.5
    p.cue(intro.id).time = 2.0                                      # then edited in CueForge
    link._on_pulled(slot, same.replace("E|1.000|", "E|1.750|", 1))   # and on the console
    assert p.cue(intro.id).time == 2.0 and "kept CueForge's" in msgs[-1]
    # the button takes the console's version anyway
    link._pull_manual = True
    link._on_pulled(slot, same.replace("E|1.000|", "E|1.750|", 1))
    assert p.cue(intro.id).time == 1.75


def test_pull_receiver_assembles_parts_over_udp():
    import time as _t
    from pythonosc.udp_client import SimpleUDPClient
    from cueforge.control.ma3pull import PullReceiver
    got = []
    r = PullReceiver(0, lambda slot, text: got.append((slot, text)))
    try:
        c = SimpleUDPClient("127.0.0.1", r.port)
        for msg in ("4:1:3:T|Hits|", "4:2:3:%0AE|3.000|Temp|On|", "4:3:3:12.12.0.5.1.1000|Hits"):
            c.send_message("/cueforge/tc", msg)
        c.send_message("/cueforge/tc", "5:1:1:same")
        t0 = _t.time()
        while len(got) < 2 and _t.time() - t0 < 3:
            _t.sleep(0.02)
    finally:
        r.close()
    assert (4, "T|Hits|%0AE|3.000|Temp|On|12.12.0.5.1.1000|Hits") in got and (5, "same") in got


def test_polling_defines_cf_pull_once_and_gives_up_without_replies(link):
    link.cfg.auto_pull = True
    link.s.engine.playing = False
    link._poll_pull()
    first = list(link.log)
    assert any("CF_P[1]=" in c for c in first) and first[-1].startswith('Lua "CF_PULL(2,false,')
    link._sender.wait_idle(5)                              # polls never pile up on a busy sender
    n = len(link.log)
    link._poll_pull()
    assert link.log[n:] == [first[-1]]                     # just the call: CF_PULL is defined already
    msgs = []
    link.status.connect(msgs.append)
    for _ in range(6):
        link._sender.wait_idle(5)
        link._poll_pull()
    assert any("no reply from the console" in m for m in msgs) and not link._pull_timer.isActive()


def test_console_reports_its_sequences(tmp_path):
    from cueforge.control.ma3pull import parse_sequences
    texts = _console_pull(tmp_path, MA_EXPORT)
    seqs = parse_sequences(texts[0])
    assert seqs == {"Song 1 Main Cues": [(1.0, "Intro"), (1.5, 'Big, "new"'), (2.0, "Verse")]}   # thousandths -> cues


def test_cues_added_or_renamed_on_the_console(link):
    from cueforge.control.ma3pull import console_cues
    from cueforge.core.editing import accept_suggestion
    s = link.s
    s._sync_engine = lambda: None
    p = s.project
    main = p.lanes[0]
    link.sync_cues()                                       # cues 1 Intro (1 s), 2 Verse (5 s) on the console
    intro, verse = p.cues_in_lane(main.id)[:2]
    seqs = {"Song 1 Main Cues": [(1.0, "Opening"), (1.5, "Big"), (2.0, "Verse"), (9.0, "Late")]}
    found = console_cues(p, seqs, link.record, [])
    assert [(c.id, n) for c, n in found["renamed"]] == [(intro.id, "Opening")]
    new = {sg.number: sg for sg in found["new"]}
    assert set(new) == {1.5, 9.0}
    assert new[1.5].time == 3.0 and new[1.5].lane_id == main.id and new[1.5].label == "Big"   # between cues 1 and 2
    assert new[9.0].time == 7.0                                                             # 2 s after the last
    # through the link: one undo step, then accepting gives the cue its console number
    msgs = []
    link.status.connect(msgs.append)
    link._check_console_cues("S|Song 1 Main Cues%0AC|1|Opening%0AC|1.5|Big%0AC|2|Verse")
    assert p.cue(intro.id).label == "Opening" and "1 cue(s) added on the console" in msgs[-1]
    sg = next(x for x in p.suggestions if x.kind == "console")
    cue = accept_suggestion(p, sg)
    assert cue.number == 1.5 and cue.label == "Big" and cue.source == "manual"
    # nothing new the next time (accepted / pending / rejected ones are remembered)
    assert console_cues(p, {"Song 1 Main Cues": [(1.5, "Big")]}, link.record, [])["new"] == []
    # a cue CueForge pushed and you deleted (delete on the console is off) is not suggested back
    p.cues = [c for c in p.cues if c.id != verse.id]
    assert console_cues(p, {"Song 1 Main Cues": [(2.0, "Verse")]}, link.record, [])["new"] == []


def test_shared_sequence_cues_of_other_songs_are_not_new(link):
    from cueforge.control.ma3pull import console_cues
    p = link.s.project
    hits = next(l for l in p.lanes if l.name == "Hits")
    s2 = p.add_song("Second")
    p.select_song(s2.id)
    p.cues.append(Cue(lane_id=hits.id, time=4.0))           # song 2's hit: cue 101 in the shared Hits
    p.select_song(p.songs[0].id)
    found = console_cues(p, {"Hits": [(1.0, ""), (101.0, ""), (102.0, "Extra")]}, {}, [])
    assert [sg.number for sg in found["new"]] == [102.0]
