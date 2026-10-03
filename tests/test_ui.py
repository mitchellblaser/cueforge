"""UI smoke/integration tests (run offscreen)."""
import os
import sys
import time
import xml.etree.ElementTree as ET

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from cueforge.analysis.pipeline import AnalysisOptions
from cueforge.core.settings import UserSettings
from cueforge.export.ltc import decode_ltc
from cueforge.ui import dialogs
from cueforge.ui.main_window import MainWindow
from cueforge.ui.session import Session
from cueforge.ui.timeline import HEADER_W, LANE_H, TOP_H, TRACK_H
from tests.synth import SR, make_click, make_song


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


def pump(app, sec=0.05):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.005)


def wait(app, cond, timeout=60):
    t = time.time()
    while not cond():
        assert time.time() - t < timeout, "timeout"
        pump(app, 0.05)


@pytest.fixture()
def win(app, tmp_path, monkeypatch):
    audio, info = make_song()
    song = tmp_path / "song.wav"
    click = tmp_path / "click.wav"
    sf.write(song, audio, SR)
    sf.write(click, make_click(info), SR)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    s = Session(UserSettings(str(tmp_path / "settings.json")))
    w = MainWindow(s)
    w.resize(1400, 900)
    w.show()
    s.import_audio([str(song), str(click)])
    wait(app, lambda: len(s.audio) == 2)
    w.activateWindow()
    w.raise_()
    QTest.qWaitForWindowActive(w, 2000)
    pump(app, 0.1)
    w.tmp = tmp_path
    w.info = info
    yield w
    s.engine.close()
    w.s.undo.mark_clean()
    w._mixer_dirty = False
    s._mixer_dirty = False
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QEvent
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    pump(app, 0.05)


_orig_key_click = QTest.keyClick


def _key_click(widget, key, *a, **k):
    """Shortcuts only fire in the active window; make sure it is before each key."""
    w = widget.window()
    if QApplication.activeWindow() is not w:
        w.activateWindow()
        QTest.qWaitForWindowActive(w, 1000)
    _orig_key_click(widget, key, *a, **k)


QTest.keyClick = _key_click


def lane_y(w, idx):
    return TOP_H + len(w.s.project.tracks) * TRACK_H + idx * LANE_H + LANE_H // 2


def test_roles_guessed(win):
    roles = {t.name: t.role for t in win.s.project.tracks}
    assert roles == {"song": "Track", "click": "Click"}
    assert len(win.mixer.strips) == 2


def test_double_click_adds_cue_and_undo(app, win):
    c = win.canvas
    c.set_view(t0=0, pps=100)
    pump(app)
    x = int(HEADER_W + 3.0 * 100)
    QTest.mouseDClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(x, lane_y(win, 0)))
    pump(app)
    assert len(win.s.project.cues) == 1
    assert abs(win.s.project.cues[0].time - 3.0) < 0.05
    win._undo()
    assert len(win.s.project.cues) == 0


def test_tap_key_and_drag(app, win):
    s = win.s
    s.engine.seek(5.0)
    QTest.keyClick(win, Qt.Key_2)
    pump(app)
    assert len(s.project.cues) == 1
    cue = s.project.cues[0]
    assert cue.lane_id == s.project.lanes[1].id and abs(cue.time - 5.0) < 0.04
    # drag it 1 s right with snapping off
    s.snap = False
    c = win.canvas
    c.set_view(t0=0, pps=100)
    pump(app)
    y = lane_y(win, 1)
    x0 = int(c.x_of(cue.time))
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y))
    QTest.mouseMove(c, QPoint(x0 + 50, y))
    QTest.mouseMove(c, QPoint(x0 + 100, y))
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, QPoint(x0 + 100, y))
    pump(app)
    assert abs(s.project.cues[0].time - 6.0) < 0.05
    # nudge with arrow keys (1 frame at 30 fps)
    t = s.project.cues[0].time
    QTest.keyClick(win, Qt.Key_Right)
    assert abs(s.project.cues[0].time - (t + 1 / 30)) < 1e-6
    QTest.keyClick(win, Qt.Key_Delete)
    assert not s.project.cues


def test_analysis_review_and_exports(app, win, monkeypatch):
    s = win.s
    done = []
    s.analysis_done.connect(done.append)
    assert s.run_analysis(AnalysisOptions(use_deep_models=False))
    wait(app, lambda: done, 120)
    pump(app, 0.1)
    assert s.project.beat_grid.source == "click track" and not s.project.beat_grid.confirmed
    assert not s.project.cues
    vis = s.project.visible_suggestions()
    assert vis
    # Tab to first suggestion, accept with A, reject next with X
    QTest.keyClick(win, Qt.Key_Tab)
    first = next(iter(s.sel_sugs))
    QTest.keyClick(win, Qt.Key_A)
    assert s.project.suggestion(first).status == "accepted"
    assert len(s.project.cues) == 1
    second = next(iter(s.sel_sugs))
    QTest.keyClick(win, Qt.Key_X)
    assert s.project.suggestion(second).status == "rejected"
    assert len(s.project.cues) == 1
    # accept all sections
    s.accept(s.visible_ids("section"))
    assert all(x.status != "pending" for x in s.project.suggestions if x.kind == "section"
               and x.confidence >= s.project.analysis.thresholds["section"])
    # decisions recorded for tuning
    assert len(s.settings.get("decisions")) >= 2

    # MA3 XML export through the dialog
    monkeypatch.setattr(dialogs.MA3ExportDialog, "exec", lambda self: (self.accept(), 1)[1])
    out = win.tmp / "show.xml"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    s.settings.set("ma3_format", "xml")
    win.export_ma3()
    root = ET.parse(out).getroot()
    assert len(root.findall(".//CmdEvent")) == len(s.project.cues)

    s.settings.set("ma3_format", "lua")
    out = win.tmp / "show.lua"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.export_ma3()
    assert "DataPool()" in out.read_text()

    out = win.tmp / "cues.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.export_csv()
    assert len(out.read_text().splitlines()) == len(s.project.cues) + 1

    monkeypatch.setattr(dialogs.LTCDialog, "exec", lambda self: (self.stereo.setChecked(True), self.accept(), 1)[2])
    out = win.tmp / "ltc_LTC.wav"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.export_ltc()
    data, sr = sf.read(out)
    assert data.shape[1] == 2
    frames = decode_ltc(data[:, 1], sr, s.project.frame_rate)
    # TC offset is 0, so there is no room for pre-roll: LTC starts at song start
    by_tc = {f[1:]: f[0] for f in frames}
    assert by_tc[(0, 0, 0, 6)] == pytest.approx(6 * sr / 30, abs=3)

    # Save + reopen keeps decisions
    path = win.tmp / "p.cueproj"
    s.project.path = str(path)
    assert win.save()
    n_cues = len(s.project.cues)
    s.open_project(str(path))
    wait(app, lambda: len(s.audio) == 2)
    assert len(s.project.cues) == n_cues
    assert s.project.suggestion(second).status == "rejected"


def test_dialogs_construct(app, win):
    for cls in (dialogs.AnalysisDialog, dialogs.TempoDialog, dialogs.ProjectSettingsDialog,
                dialogs.MA3ExportDialog, dialogs.LTCDialog, dialogs.AudioDeviceDialog):
        d = cls(win.s, win)
        assert isinstance(d, QDialog)
        d.deleteLater()
    dialogs.ShortcutsDialog(win).deleteLater()
    c = win.s.add_cue(win.s.project.lanes[0].id, 1.0)
    d = dialogs.CueDialog(win.s, c.id, win)
    d.label.setText("Hello")
    d.tc.setText("00:00:02:15")
    d.accept()
    assert c.label == "Hello" and abs(c.time - 2.5) < 1e-6


def test_tempo_dialog_sets_grid(app, win):
    d = dialogs.TempoDialog(win.s, win)
    d.bpm.setValue(128)
    d.first.setText("00:00:00:15")
    d.accept()
    g = win.s.project.beat_grid
    assert g.confirmed and abs(g.bpm() - 128) < 0.01 and abs(g.downbeats[0] - 0.5) < 1e-6


def test_mixer_strip_controls_engine(app, win):
    tid = win.s.project.tracks[0].id
    strip = win.mixer.strips[tid]
    strip.mute.click()
    assert win.s.engine.tracks[tid].mute
    strip.fader.setValue(-120)
    assert win.s.project.track(tid).gain_db == pytest.approx(-12)
    win.mixer.click_on.click()
    assert win.s.project.mixer.click_enabled and win.s.engine.click_enabled


def test_tap_along_grid(app, win, monkeypatch):
    s = win.s
    win.a_tapgrid.setChecked(True)          # starts tap mode (and playback)
    taps = [16.0 + 0.5 * k + (0.03 if k % 2 else -0.025) for k in range(12)]  # verse: drums playing
    for t in taps:
        monkeypatch.setattr(s.engine, "position", lambda t=t: t)
        QTest.keyClick(win, Qt.Key_T)
    s.engine.pause()
    win.a_tapgrid.setChecked(False)         # builds the grid
    g = s.project.beat_grid
    assert g.confirmed and "tapped" in g.source
    assert abs(g.bpm() - 120) < 3
    # taps were snapped onto the click/drum onsets (song beats are every 0.5 s from 0.0)
    near = [min(abs(b - x) for x in win.info["beats"]) for b in g.beats if 16.0 <= b <= 21.5]
    assert np.median(near) < 0.012
    win.s.halve_tempo()
    assert abs(s.project.beat_grid.bpm() - 60) < 2
    win._undo()
    assert abs(s.project.beat_grid.bpm() - 120) < 3


def test_chase_steps_and_hold(app, win):
    from cueforge.core.model import Suggestion
    s = win.s
    sg = Suggestion("melody", 2.0, 0.9, "4 notes", label="Lead phrase: rising line", duration=1.5,
                    steps=[2.0, 2.4, 2.8, 3.2], idea="chase")
    with s.edit("test"):
        from cueforge.core import editing
        editing.merge_suggestions(s.project, [sg], {"melody"})
    assert s.project.lane(sg.lane_id).name == "FX / Chase"
    n = s.accept_as_steps(sg.id)
    assert n == 4 and len(s.project.cues_in_lane(sg.lane_id)) == 4
    fill = Suggestion("fill", 5.0, 0.9, "groove break", label="Drum fill (2 beats)", duration=1.0)
    with s.edit("test"):
        editing.merge_suggestions(s.project, [fill], {"fill"})
    s.accept([fill.id])
    cue = s.project.cue(fill.cue_id)
    assert s.project.lane(cue.lane_id).name == "Strobe" and cue.duration == pytest.approx(1.0)
    pump(app, 0.1)
    win.canvas.repaint()


def test_setlist_sidebar(app, win):
    s = win.s
    first = s.project.song.id
    c1 = s.add_cue(s.project.lanes[0].id, 1.0, label="first song cue")
    # add a second song from the click file (as if dropped on the setlist)
    click = str(win.tmp / "click.wav")
    sid = s.add_song([click])
    wait(app, lambda: len([t for t in s.project.tracks if t.id in s.audio]) == 1)
    assert s.project.song.id == sid and s.project.song.name == "click"
    assert win.setlist.list.count() == 2
    assert set(s.engine.tracks) == {t.id for t in s.project.tracks}   # only this song's audio plays
    assert not s.project.cues
    s.add_cue(s.project.lanes[0].id, 2.0, label="second song cue")
    # switching back via the sidebar restores the first song
    win.setlist.list.setCurrentRow(0)
    pump(app)
    assert s.project.song.id == first and [c.label for c in s.project.cues] == ["first song cue"]
    assert set(s.engine.tracks) == {t.id for t in s.project.tracks}
    # undo of the cue added in song two jumps back to song two
    win._undo()
    assert s.project.song.id == sid and not s.project.cues
    # song settings / auto numbering
    s.auto_number_setlist()
    assert [x.tc_offset for x in s.project.songs] == [3600.0, 7200.0]
    assert [x.cue_start for x in s.project.songs] == [1.0, 101.0]
    win._step_song(-1)
    assert s.project.song.id == first
    # analysis results land in the song that was analysed, even after switching away
    from cueforge.analysis.pipeline import AnalysisOptions
    done = []
    s.analysis_done.connect(done.append)
    assert s.run_analysis(AnalysisOptions(use_deep_models=False, hits=False, harmony=False, melody=False))
    s.switch_song(sid)
    wait(app, lambda: done, 120)
    assert s.project.song.id == sid
    assert s.project.song_by_id(first).suggestions and not s.project.song_by_id(sid).suggestions
    # save / reopen keeps the setlist
    path = win.tmp / "set.cueproj"
    s.project.path = str(path)
    assert win.save()
    s.open_project(str(path))
    assert [x.name for x in s.project.songs] == ["Song 1", "click"]


def test_cue_and_temp_buttons(app, win, monkeypatch):
    s = win.s
    c = win.canvas
    c.set_view(t0=0, pps=100)
    pump(app)
    # click the Strobe lane header -> active lane
    strobe = next(l for l in s.project.lanes if l.name == "Strobe")
    idx = s.project.lanes.index(strobe)
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(40, lane_y(win, idx)))
    pump(app)
    assert s.active_lane_id == strobe.id
    assert win.lane_combo.currentData() == strobe.id
    # Q drops a cue, W a Temp with the hold from the toolbar, at the (heard) playhead
    monkeypatch.setattr(s.engine, "position", lambda: 4.0)
    QTest.keyClick(win, Qt.Key_Q)
    win.hold_spin.setValue(0.75)
    monkeypatch.setattr(s.engine, "position", lambda: 6.0)
    QTest.keyClick(win, Qt.Key_W)
    cues = s.project.cues_in_lane(strobe.id)
    assert [round(x.time, 3) for x in cues] == [4.0, 6.0]
    assert cues[0].duration is None and cues[1].duration == pytest.approx(0.75)
    assert s.temp_hold == pytest.approx(0.75)
    # editing the Hold box with the Temp selected changes its hold
    win.hold_spin.setValue(1.0)
    assert s.project.cue(cues[1].id).duration == pytest.approx(1.0)
    # ↓ moves the active lane
    QTest.keyClick(win, Qt.Key_Down)
    assert s.active_lane_id == s.project.lanes[idx + 1].id
    # toolbar buttons do the same as the keys
    monkeypatch.setattr(s.engine, "position", lambda: 8.0)
    win.a_add_temp.trigger()
    lane2 = s.project.lanes[idx + 1].id
    assert s.project.cues_in_lane(lane2)[0].duration == pytest.approx(1.0)
    # convert: make the plain cue a Temp and back
    s.select(cues={cues[0].id})
    QTest.keyClick(win, Qt.Key_W, Qt.ShiftModifier)
    assert s.project.cue(cues[0].id).duration == pytest.approx(1.0)
    QTest.keyClick(win, Qt.Key_Q, Qt.ShiftModifier)
    assert s.project.cue(cues[0].id).duration is None
    # export: Temp -> Temp On + Temp Off, cue -> Goto
    from cueforge.export.ma3 import build_ma3_xml
    root = ET.fromstring(build_ma3_xml(s.project))
    tokens = [(e.find("RealtimeCmd").get("Token"), e.find("RealtimeCmd").get("Status"))
              for e in root.findall(".//CmdEvent")]
    assert ("Goto", "On") in tokens and ("Temp", "On") in tokens and ("Temp", "Off") in tokens
    assert sum(1 for t in tokens if t == ("Temp", "Off")) == 2


def test_dual_monitor_and_popout(app, win):
    win.dual_monitor(True)
    pump(app)
    pw = win.panels_window
    assert pw is not None and pw.isVisible()
    assert win.dock_sugs.parent() is pw and win.dock_mixer.parent() is pw
    # shortcuts still work while the panels window has focus
    win.s.engine.seek(3.0)
    pw.activateWindow()
    QTest.keyClick(pw, Qt.Key_Q)
    assert any(abs(c.time - 3.0) < 0.05 for c in win.s.project.cues)
    win.dual_monitor(False)
    pump(app)
    assert win.panels_window is None and win.dock_sugs.parent() is win
    win.pop_out(win.dock_cues)
    assert win.dock_cues.isFloating()
    win.reset_layout()
    assert not win.dock_cues.isFloating()


def test_sections_copy_paste_ui(app, win):
    from cueforge.core.model import BeatGrid
    s = win.s
    s.set_grid(BeatGrid.from_tempo(120, 0.0, 70))
    s.engine.seek(0.0)
    s.add_section_at(0.0, "Verse 1")
    s.add_section_at(16.0, "Chorus 1")
    s.add_section_at(32.0, "Verse 2")
    s.add_section_at(48.0, "Chorus 2")
    ch1 = next(m for m in s.project.sections if m.name == "Chorus 1")
    lane = s.project.lanes[0].id
    for t in (16.0, 18.5, 20.0):
        s.add_cue(lane, t)
    assert s.copy_section_to_repeats(ch1.id) == 3
    assert sorted(round(c.time, 2) for c in s.project.cues if c.time >= 48) == [48.0, 50.5, 52.0]
    win._undo()
    assert not [c for c in s.project.cues if c.time >= 48]
    # copy two cues, paste at the playhead with Ctrl+V
    s.select(cues={c.id for c in s.project.cues if c.time in (18.5, 20.0)})
    QTest.keyClick(win, Qt.Key_C, Qt.ControlModifier)
    s.engine.seek(60.0)
    QTest.keyClick(win, Qt.Key_V, Qt.ControlModifier)
    assert sorted(round(c.time, 2) for c in s.project.cues if c.time >= 59) == [60.0, 61.5]
    # aligned paste into Chorus 2 (keeps the position inside the section)
    s.engine.seek(49.0)
    QTest.keyClick(win, Qt.Key_V, Qt.ControlModifier | Qt.ShiftModifier)
    assert sorted(round(c.time, 2) for c in s.project.cues if 48 <= c.time < 59) == [50.5, 52.0]
    # M adds a section marker at the playhead; rename all repeats
    s.engine.seek(64.0)
    QTest.keyClick(win, Qt.Key_M)
    assert any(abs(m.time - 64.0) < 0.05 for m in s.project.sections)
    s.rename_section(ch1.id, "Hook", all_of_kind=True)
    assert sorted(m.name for m in s.project.sections if m.kind == "hook") == ["Hook 1", "Hook 2"]
    # pattern fill over a section
    r = s.section_range(next(m for m in s.project.sections if m.name == "Verse 2").id)
    n = s.pattern_fill(s.project.lanes[2].id, r[0], r[1], "beat", temp=True)
    assert n == 32 and all(c.duration for c in s.project.cues_in_lane(s.project.lanes[2].id))
    win.canvas.repaint()


def test_midi_control_and_feedback(app, win):
    import mido
    from cueforge.control.actions import WHITE, color_velocity
    hub = win.control
    s = win.s
    sent = []

    class FakeOut:
        def send(self, m):
            sent.append(m)

        def close(self):
            pass
    hub._out = FakeOut()
    lanes = s.project.lanes
    # note 36 -> cue into lane 1; note 45 -> Temp into lane 2 (hold from the Hold box)
    hub.handle_midi(mido.Message("note_on", note=36, velocity=100), 2.0)
    hub.handle_midi(mido.Message("note_on", note=45, velocity=100), 3.0)
    hub.handle_midi(mido.Message("note_off", note=45), 3.4)
    pump(app)
    c1 = s.project.cues_in_lane(lanes[0].id)
    c2 = s.project.cues_in_lane(lanes[1].id)
    assert [round(c.time, 2) for c in c1] == [2.0] and c1[0].duration is None
    assert [round(c.time, 2) for c in c2] == [3.0] and c2[0].duration == pytest.approx(s.temp_hold)
    assert any(m.velocity == WHITE for m in sent)               # pad flashed on the hit
    # hold time = how long the pad is held
    hub.cfg.hold_from_press = True
    hub.handle_midi(mido.Message("note_on", note=46, velocity=127), 5.0)
    hub.handle_midi(mido.Message("note_on", note=46, velocity=0), 5.8)
    pump(app)
    c3 = s.project.cues_in_lane(lanes[2].id)
    assert c3[0].duration == pytest.approx(0.8, abs=0.04)
    # colour feedback follows lane colours; active lane's Temp pad is full colour
    s.set_active_lane(lanes[0].id)
    msgs = {m.note: m.velocity for m in hub.feedback_messages()}
    assert msgs[36] == color_velocity(lanes[0].color)
    assert msgs[44] == color_velocity(lanes[0].color)               # active lane temp pad: full
    assert msgs[45] == color_velocity(lanes[1].color, dim=True)     # other temp pads dimmer
    assert msgs[36 + len(lanes)] == 0 if len(lanes) < 8 else True   # pads beyond the lanes are off
    # MIDI learn
    got = []
    hub.learned.connect(got.append)
    hub.start_learn()
    hub.handle_midi(mido.Message("control_change", channel=2, control=20, value=127), 0.0)
    pump(app)
    assert got and got[0].kind == "cc" and got[0].number == 20 and got[0].channel == 2
    hub._out = None


def test_osc_control_roundtrip(app, win):
    import socket
    from pythonosc.osc_message import OscMessage
    from pythonosc.udp_client import SimpleUDPClient
    hub = win.control
    s = win.s
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    hub.cfg.osc_enabled = True
    hub.cfg.osc_port = 0                       # any free port
    hub.cfg.osc_feedback_host = "127.0.0.1"
    hub.cfg.osc_feedback_port = rx.getsockname()[1]
    hub.apply()
    try:
        assert hub.osc_port
        # feedback arrives with the lane names
        names = set()
        for _ in range(40):
            try:
                data, _ = rx.recvfrom(4096)
            except socket.timeout:
                break
            msg = OscMessage(data)
            if msg.address.endswith("/name"):
                names.add(msg.params[0])
            if len(names) >= 3:
                break
        assert "Main Cues" in names
        cli = SimpleUDPClient("127.0.0.1", hub.osc_port)
        s.engine.seek(7.0)
        cli.send_message("/cueforge/lane/2/temp", 1.0)
        wait(app, lambda: len(s.project.cues) >= 1, 5)
        s.engine.seek(9.0)
        cli.send_message("/cueforge/cue", 3)
        wait(app, lambda: len(s.project.cues) >= 2, 5)
        lanes = s.project.lanes
        assert [round(c.time, 1) for c in s.project.cues_in_lane(lanes[1].id)] == [7.0]
        assert s.project.cues_in_lane(lanes[1].id)[0].duration
        assert [round(c.time, 1) for c in s.project.cues_in_lane(lanes[2].id)] == [9.0]
        ids = [l.id for l in lanes]
        expect = ids[min(len(ids) - 1, ids.index(s.active_lane_id) + 1)]
        cli.send_message("/cueforge/lane/next", [])
        wait(app, lambda: s.active_lane_id == expect, 5)
    finally:
        hub.close()
        rx.close()


def test_import_ma3_ui(app, win, monkeypatch):
    from cueforge.export.ma3 import export_ma3_xml_all
    s = win.s
    s.add_cue(s.project.lanes[0].id, 2.0, label="A")
    s.add_song(name="Second")
    s.add_cue(s.project.lanes[0].id, 4.0, label="B")
    paths = export_ma3_xml_all(s.project, str(win.tmp / "tc"))
    # wipe the cues, then bring them back from the console files
    for song in s.project.songs:
        song.cues = []
    win.import_ma3(paths, replace=True)
    assert [c.label for c in s.project.songs[0].cues] == ["A"]
    assert [c.label for c in s.project.songs[1].cues] == ["B"]
