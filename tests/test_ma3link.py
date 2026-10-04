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
    seq_main = s.project.lanes[0].ma3_sequence
    seq_hits = s.project.lanes[1].ma3_sequence
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
    assert msgs[0] == ("/gma3/cmd", f"Goto Sequence {seq_main} Cue 1")
    assert len(msgs) == 4


def test_jump_resyncs_and_stop_releases(link):
    s = link.s
    eng = s.engine
    seq_main = s.project.lanes[0].ma3_sequence
    seq_hits = s.project.lanes[1].ma3_sequence
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
    seq = main.ma3_sequence
    cmds = link.push_all()
    assert f"Store Sequence {seq} Cue 1 /Merge /NoConfirm" in cmds
    assert f'Label Sequence {seq} Cue 1 "Intro"' in cmds
    assert f'Label Sequence {seq} Cue 2 "Verse"' in cmds
    assert f'Label Sequence {seq} "{main.name}"' in cmds
    assert cmds.index(f'Label Sequence {seq} "{main.name}"') > cmds.index(f"Store Sequence {seq} Cue 1 /Merge /NoConfirm")
    # numbers are now fixed: a cue inserted in between gets 1.1 and nothing shifts
    assert all(c.number is not None for c in p.cues)
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
    assert any("Cue 101" in c for c in link.sync_cues())
    link.cfg.all_songs = False
    p.lanes[1].export = False
    assert link.sync_cues() == []


def test_undo_keeps_pinned_numbers(link):
    """Review #4: undo restoring unnumbered cues must not shift console cue numbers."""
    p = link.s.project
    main = p.lanes[0]
    seq = main.ma3_sequence
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
    seq_hits = s.project.lanes[1].ma3_sequence
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


def test_timecode_write_error_is_reported(link, tmp_path):
    """Review #8: an unwritable library folder gives a status message, not an exception."""
    msgs = []
    link.status.connect(msgs.append)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    link.cfg.timecode_dir = str(blocker)               # a file, not a folder
    assert link.push_timecode() == []
    assert msgs and ("failed" in msgs[-1] or "folder" in msgs[-1].lower())


def test_custom_templates(link):
    link.cfg.cmd_go = "Go Sequence {seq}"
    link.cfg.cmd_temp_off = "Off Sequence {seq}"
    link.cfg.cmd_goto = "broken {nope}"            # bad template falls back to the default
    assert link.cmd("go", 3) == "Go Sequence 3"
    assert link.cmd("temp_off", 3) == "Off Sequence 3"
    assert link.cmd("goto", 3, 4.5) == "Goto Sequence 3 Cue 4.5"


def test_timecode_push_writes_library_file(link, tmp_path):
    p = link.s.project
    slot = p.song.ma3_timecode
    cmds = link.push_timecode()
    assert cmds == [f"Delete Timecode {slot} /NoConfirm", f'Import Timecode {slot} /File "CueForge_{slot}.xml" /NoConfirm']
    f = tmp_path / "timecodes" / f"CueForge_{slot}.xml"
    root = ET.parse(f).getroot()
    assert root.find(".//Timecode") is not None or root.tag == "Timecode"
    msgs = received(link.rx)
    assert [m[1] for m in msgs][-2:] == cmds


def test_all_songs_sync(link):
    s = link.s
    p = s.project
    p.add_song("Second")
    song2 = p.songs[-1]
    p.select_song(song2.id)
    p.cues.append(Cue(lane_id=p.lanes[0].id, time=2.0, label="S2"))
    link.cfg.all_songs = True
    cmds = link.push_all()
    nums = {c for c in cmds if c.startswith("Store")}
    assert any(f"Cue {song2.cue_start:g} " in c for c in nums)        # second song's cue range
    assert any(" Cue 1 " in c for c in nums)                          # first song too


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
    stores = [c for c in cmds if c.startswith("Store Sequence %d " % strobe.ma3_sequence)]
    assert len(stores) == 1
    assert sum(1 for c in cmds if c.startswith("Label Sequence %d Cue" % strobe.ma3_sequence)) == 1
    macro = [c for c in build_ma3_macro_commands(p) if c.startswith("Store Sequence %d " % strobe.ma3_sequence)]
    assert len(macro) == 1
    # the timecode track fires Temp On / Off on that one cue every time
    xml = build_ma3_xml(p)
    root = ET.fromstring(xml)
    temps_ev = [e for e in root.iter("RealtimeCmd") if e.get("Token") == "Temp"]
    assert len(temps_ev) == 2 * len(temps) and len({e.get("Cue") for e in temps_ev}) == 1
    # editing a Temp's number moves the shared cue
    s._sync_engine = lambda: None                                               # no audio here
    s.update_cue(temps[-1].id, number=20.0)
    assert set(effective_cue_numbers(p, strobe.id).values()) == {20.0}


def test_link_test_command():
    from cueforge.ui.link_dialog import TEST_CMD
    assert TEST_CMD.startswith("Lua ") and "Printf" in TEST_CMD
