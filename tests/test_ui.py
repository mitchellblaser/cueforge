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


def test_ma3_link_dialog(app, win):
    from cueforge.ui.link_dialog import LinkDialog
    d = LinkDialog(win.ma3link, win)
    d.enabled.setChecked(True)
    d.port.setValue(9)                        # discard port: nothing listens
    d.tpl["go"].setText("Go Sequence {seq}")
    d.accept()
    assert win.ma3link.active and win.ma3link.cfg.cmd_go == "Go Sequence {seq}"
    win._link_badge()
    assert "MA3" in win.link_label.text()
    win.ma3link.cfg.enabled = False
    win.ma3link.apply()


def test_temp_release_drag_and_undo(app, win):
    s = win.s
    s.snap = False
    c = win.canvas
    c.set_view(t0=0, pps=100)
    pump(app)
    lane = s.project.lanes[2]
    cue = s.add_cue(lane.id, 3.0)
    s.update_cue(cue.id, duration=0.5)
    pump(app)
    idx = [l.id for l in s.project.lanes].index(lane.id)
    y = lane_y(win, idx) + 12                  # lower part of the lane, over the hold bar
    xe = int(c.x_of(3.5))
    assert c.hit_temp_end(QPoint(xe, y)) == cue.id
    assert c.hit_temp_end(QPoint(int(c.x_of(3.0)), y)) is None      # the start is the cue, not the end
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, QPoint(xe, y))
    QTest.mouseMove(c, QPoint(xe + 60, y))
    QTest.mouseMove(c, QPoint(xe + 100, y))
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, QPoint(xe + 100, y))
    pump(app)
    got = s.project.cue(cue.id)
    assert abs(got.duration - 1.5) < 0.02 and abs(got.time - 3.0) < 1e-6     # start didn't move
    win._undo()
    assert abs(s.project.cue(cue.id).duration - 0.5) < 1e-6


def test_cue_list_follows_playhead(app, win):
    s = win.s
    a, b = s.project.lanes[0], s.project.lanes[1]
    c1 = s.add_cue(a.id, 1.0)
    c2 = s.add_cue(a.id, 4.0)
    t1 = s.add_cue(b.id, 2.0)
    s.update_cue(t1.id, duration=1.0)
    table = win.cue_table
    table.rebuild_now()
    state, latest = table.active_cues(2.2)
    assert state.get(c1.id) == "on" and state.get(t1.id) == "fired" and latest == t1.id
    state, latest = table.active_cues(3.5)
    assert state == {c1.id: "on"}                     # the Temp released at 3.0
    state, _ = table.active_cues(4.1)
    assert state.get(c2.id) == "fired" and c1.id not in state
    s.engine.seek(4.1)
    table._light(force=True)
    row = table._rows[c2.id]
    bg = table.model.data(table.model.index(row, 0), Qt.BackgroundRole)
    assert bg is not None and bg.color().alpha() > 0
    n_undo = len(s.undo._undo) if hasattr(s.undo, "_undo") else None
    table._light(force=True)                          # styling is not an edit
    if n_undo is not None:
        assert len(s.undo._undo) == n_undo


def test_bar_one_key_and_filters_count(app, win):
    s = win.s
    from cueforge.core.model import BeatGrid, Suggestion
    s.set_grid(BeatGrid.from_tempo(120, 0.0, 30.0, 4))
    s.engine.seek(2.55)
    _key_click(win, Qt.Key_D)
    pump(app)
    g = s.project.beat_grid
    assert g.bar_one == 2.5 and g.confirmed and 2.5 in g.downbeats
    # the setlist counts only suggestions the filters show
    s.project.suggestions = [Suggestion(kind="hit", time=1.0, confidence=0.9, reason="t"),
                             Suggestion(kind="fill", time=2.0, confidence=0.9, reason="t")]
    s.set_filter("hit", visible=False)
    pump(app)
    text = win.setlist.list.item(0).text()
    assert "1 AI to review" in text
    assert "hidden by the Suggestion filters" in win.setlist.list.item(0).toolTip()
    # Reject hidden rejects only what the filters hide
    win.suggestions._reject_hidden()
    assert [x.status for x in s.project.suggestions] == ["rejected", "pending"]


def test_inspector_tabs_rejoin(app, win):
    win.pop_out(win.dock_cues)
    pump(app, 0.2)
    win.dock_cues.setFloating(False)
    pump(app, 0.3)
    assert set(win.tabifiedDockWidgets(win.dock_sugs)) == {win.dock_cues, win.dock_lanes}


def test_bulk_import_and_queue(app, win, monkeypatch):
    s = win.s
    import shutil
    folder = win.tmp / "set"
    (folder / "Two").mkdir(parents=True)
    shutil.copy(win.tmp / "song.wav", folder / "One.wav")
    shutil.copy(win.tmp / "song.wav", folder / "Two" / "Two mix.wav")
    shutil.copy(win.tmp / "click.wav", folder / "Two" / "click.wav")
    from cueforge.core.bulk import song_groups_from_folder
    ids = s.add_songs(song_groups_from_folder(str(folder)))
    assert [s.project.song_by_id(i).name for i in ids] == ["One", "Two"]
    two = s.project.song_by_id(ids[1])
    assert {t.name: t.role for t in two.tracks} == {"Two mix": "Track", "click": "Click"}
    from cueforge.ui.dialogs import AnalysisDialog
    dlg = AnalysisDialog(s, win, scope="all", song_ids=ids)
    assert dlg.song_ids() == ids
    dlg.scope.setCurrentIndex(dlg.scope.findData("all"))
    assert len(dlg.song_ids()) == 3
    from cueforge.analysis.pipeline import AnalysisOptions
    done, prog = [], []
    s.analysis_done.connect(done.append)
    s.progress.connect(lambda f, m: prog.append(m))
    assert s.run_analysis(AnalysisOptions(use_deep_models=False, hits=False, harmony=False, melody=False,
                                          fills=False), ids)
    assert not s.run_analysis(AnalysisOptions(), ids)          # one queue at a time
    wait(app, lambda: done, 240)
    assert not isinstance(done[0], str), done[0]
    assert "Analysed 2 of 2 songs" in done[0].log[-1]
    assert all(s.project.song_by_id(i).analysed for i in ids)
    assert all(s.project.song_by_id(i).suggestions for i in ids)
    assert any("Song 2/2" in m for m in prog)
    assert two.beat_grid.source == "click track"


def test_cancel_background_analysis(app, win):
    s = win.s
    from cueforge.analysis.pipeline import AnalysisOptions
    done = []
    s.analysis_done.connect(done.append)
    assert s.run_analysis(AnalysisOptions(use_deep_models=False))
    pump(app, 1.0)
    s.cancel_analysis()
    wait(app, lambda: not s.analysis_running(), 15)
    assert not done and not s.project.suggestions


def test_cuepoints_dialog_import_and_undo(app, win):
    s = win.s
    p = win.tmp / "cp.txt"
    p.write_text("Track\tType\tPosition\tCue No\tLabel\tFade\n"
                 "Song 1\tLighting\t00:00:01:00\t5\tIntro\t0\n"
                 "Song 1\tLighting\t00:00:03:00\t6\tVerse\t0\n"
                 "New Tune\tLighting\t02:00:00:00\t1\tGo\t0\n")
    from cueforge.ui.cuepoints_dialog import CuePointsImportDialog
    dlg = CuePointsImportDialog(s, str(p), win)
    assert dlg.preview.rowCount() == 3 and "(new)" in dlg.preview.item(2, 0).text()
    items, problems = dlg.items()
    r = s.import_cuepoints(items, dlg.options())
    assert r["cues"] == 3 and r["songs_created"] == ["New Tune"]
    assert [round(c.time, 2) for c in s.project.cues] == [1.0, 3.0]
    nt = next(x for x in s.project.songs if x.name == "New Tune")
    assert nt.tc_offset == 7200.0 and len(nt.cues) == 1 and nt.cues[0].time == 0.0
    win._undo()                                       # undo the last song's part: New Tune
    assert not nt.cues
    win._undo()
    assert not s.project.cues


def test_ai_models_dialog_and_first_run(app, win, monkeypatch):
    from cueforge.ui import ai_models_dialog as amd
    d = amd.AIModelsDialog(win.s.settings, win)
    assert set(d.boxes) == {"beat_this", "demucs"} and d.keys() == ["beat_this", "demucs"]
    d.close()
    shown = []
    monkeypatch.delenv("CUEFORGE_NO_FIRST_RUN", raising=False)
    monkeypatch.setattr(amd.addons, "any_installed", lambda: False)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text()) or 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[-1])   # "Don't ask again"
    amd.first_run_prompt(win.s.settings, win)
    assert shown and win.s.settings.get("ai_prompt_done")
    shown.clear()
    amd.first_run_prompt(win.s.settings, win)        # not asked twice
    assert not shown


def test_new_project_cancels_analysis(app, win):
    import glob
    import tempfile
    s = win.s
    from cueforge.analysis.pipeline import AnalysisOptions
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "cueforge-analysis-*")))
    assert s.run_analysis(AnalysisOptions(use_deep_models=False))
    pump(app, 0.8)
    s.new_project()
    assert not s.analysis_running()
    pump(app, 0.5)
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "cueforge-analysis-*"))) <= before
    assert s.run_analysis(AnalysisOptions()) is False        # nothing to analyse in the new project


def test_auto_advance_toggle(app, win):
    from cueforge.core.model import Suggestion
    s = win.s
    s.project.suggestions = [Suggestion("hit", 1.0, 0.9, "x", lane_id=s.project.lanes[1].id),
                             Suggestion("hit", 2.0, 0.9, "x", lane_id=s.project.lanes[1].id)]
    first = s.project.suggestions[0].id
    win.a_advance.setChecked(False)
    assert not s.auto_advance and not win.suggestions.advance.isChecked()
    s.select(sugs={first})
    win.accept_selected()
    assert not s.sel_sugs                               # stayed put
    s.select(sugs={s.project.suggestions[1].id})
    win.a_advance.setChecked(True)
    assert win.suggestions.advance.isChecked()


def test_enter_in_cue_table_commits_not_restart(app, win):
    s = win.s
    c = s.add_cue(s.project.lanes[0].id, 2.0)
    s.engine.seek(7.0)
    ct = win.cue_table
    win._show_panel(win.dock_cues)
    ct.rebuild_now()
    pump(app, 0.1)
    idx = ct.model.index(ct._rows[c.id], 3)
    ct.table.setCurrentIndex(idx)
    ct.table.edit(idx)
    pump(app, 0.1)
    from PySide6.QtWidgets import QLineEdit
    ed = ct.table.findChild(QLineEdit)
    assert ed is not None
    ed.setFocus()
    QTest.keyClicks(ed, "Drop")
    _orig_key_click(ed, Qt.Key_Return)
    pump(app, 0.2)
    assert s.project.cue(c.id).label == "Drop"
    assert abs(s.engine.position() - 7.0) < 0.05          # Return did not jump to the start
    # the automatic cue number is shown (dim) without being stored
    ct.rebuild_now()
    assert ct.model.data(ct.model.index(ct._rows[c.id], 2)) == "1" and s.project.cue(c.id).number is None


def test_hold_lane_key_makes_snapped_temp(app, win):
    s = win.s
    from cueforge.core.model import BeatGrid
    s.set_grid(BeatGrid.from_tempo(120, 0.0, 30.0, 4))
    s.snap = True
    lane = s.project.lanes[2]
    s.engine.seek(1.02)
    s.engine.play()
    try:
        pump(app, 0.1)
        win._tap_press(lane.id, lane.tap_key)
        pump(app, 0.8)
        hp = win.canvas.hold_preview
        assert hp and hp[0][0] == lane.id and hp[0][2] - hp[0][1] > 0.2   # growing while held (overlay)
        win._tap_release(lane.tap_key)
    finally:
        s.engine.pause()
    cue = s.project.cues_in_lane(lane.id)[0]
    assert abs(cue.time / 0.5 - round(cue.time / 0.5)) < 0.04          # start on a beat
    end = cue.time + cue.duration
    assert abs(end / 0.5 - round(end / 0.5)) < 0.04 and cue.duration >= 0.5 - 1e-6
    # a quick tap stays a normal cue
    s.engine.seek(5.0)
    s.engine.play()
    try:
        win._tap_press(lane.id, lane.tap_key)
        win._tap_release(lane.tap_key)
    finally:
        s.engine.pause()
    assert s.project.cues_in_lane(lane.id)[-1].duration is None
    # playback stops while the key is held: the Temp is finished (snapped), not left half-done
    s.engine.seek(8.02)
    s.engine.play()
    try:
        win._tap_press(lane.id, lane.tap_key)
        pump(app, 0.7)
    finally:
        s.engine.pause()
    pump(app, 0.1)
    held = [c for c in s.project.cues_in_lane(lane.id) if c.time > 7.5][0]
    assert held.duration and abs((held.time + held.duration) / 0.5 - round((held.time + held.duration) / 0.5)) < 0.04
    assert not win._held and not win.canvas.hold_preview
    # hold 3 and tap 2 meanwhile: 2 is a normal cue, the Temp on 3 carries on until 3 is released
    other = s.project.lanes[3]
    s.engine.seek(12.02)
    s.engine.play()
    try:
        win._tap_press(lane.id, lane.tap_key)
        pump(app, 0.4)
        win._tap_press(other.id, other.tap_key)
        win._tap_release(other.tap_key)
        assert win._held                                      # 3 is still held
        pump(app, 0.4)
        win._tap_release(lane.tap_key)
    finally:
        s.engine.pause()
    first = [c for c in s.project.cues_in_lane(lane.id) if c.time > 11.5][0]
    second = [c for c in s.project.cues_in_lane(other.id) if c.time > 11.5][0]
    assert first.duration and first.duration >= 0.5 - 1e-6 and second.duration is None


def test_reorder_lanes_keys_follow(app, win):
    s = win.s
    names = [l.name for l in s.project.lanes]
    s.move_lane_to(s.project.lanes[3].id, 0)
    assert [l.name for l in s.project.lanes][0] == names[3]
    assert [l.tap_key for l in s.project.lanes[:5]] == ["1", "2", "3", "4", "5"]
    win._undo()
    assert [l.name for l in s.project.lanes] == names
    # dragging a lane header on the timeline
    c = win.canvas
    c.set_view(t0=0, pps=100)
    pump(app)
    y0, y1 = lane_y(win, 0), lane_y(win, 2) + 10
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, QPoint(40, y0))
    QTest.mouseMove(c, QPoint(40, y0 + 20))
    QTest.mouseMove(c, QPoint(40, y1))
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, QPoint(40, y1))
    pump(app)
    assert [l.name for l in s.project.lanes][:3] == [names[1], names[2], names[0]]


def test_section_resize_and_move(app, win):
    s = win.s
    s.snap = False
    from cueforge.core.model import SectionMarker
    s.project.sections = [SectionMarker("Verse", 0.0), SectionMarker("Chorus", 4.0)]
    c = win.canvas
    c.set_view(t0=0, pps=100)
    pump(app)
    from cueforge.ui.timeline import RULER_H, SECTION_H
    y = RULER_H + SECTION_H // 2
    # drag the Chorus start edge (= Verse end) from 4 s to 5 s
    x = int(c.x_of(4.0))
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
    QTest.mouseMove(c, QPoint(x + 50, y))
    QTest.mouseMove(c, QPoint(x + 100, y))
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, QPoint(x + 100, y))
    pump(app)
    ch = next(m for m in s.project.sections if m.name == "Chorus")
    assert abs(ch.time - 5.0) < 0.05, (c.pps, c.t0, x, [(m.name, m.time) for m in s.project.sections])
    # move the Chorus body 1 s earlier: it keeps its length, the Verse gives way
    from cueforge.core.arrange import section_bounds
    a, b = section_bounds(s.project, ch, s.duration)
    x = int(c.x_of(a + 1.0))
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
    QTest.mouseMove(c, QPoint(x - 50, y))
    QTest.mouseMove(c, QPoint(x - 100, y))
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, QPoint(x - 100, y))
    pump(app)
    ch = next(m for m in s.project.sections if m.name == "Chorus")
    a2, b2 = section_bounds(s.project, ch, s.duration)
    assert abs(a2 - (a - 1.0)) < 0.05 and abs((b2 - a2) - (b - a)) < 0.05
    # it can't be pushed past the end of the song
    n_undo = len(s.undo._undo) if hasattr(s.undo, "_undo") else None
    x = int(c.x_of(a2 + 1.0))
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
    QTest.mouseMove(c, QPoint(x + 300, y))
    QTest.mouseMove(c, QPoint(x + 600, y))
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, QPoint(x + 600, y))
    pump(app)
    ch = next(m for m in s.project.sections if m.name == "Chorus")
    a3, b3 = section_bounds(s.project, ch, s.duration)
    assert b3 <= s.duration + 1e-6 and abs((b3 - a3) - (b - a)) < 0.05
    if n_undo is not None and len(s.undo._undo) > n_undo:
        win._undo()                                   # the push to the end (if it moved at all)
    ch = next(m for m in s.project.sections if m.name == "Chorus")
    assert abs(section_bounds(s.project, ch, s.duration)[0] - a2) < 0.05
    win._undo()                                       # the move 1 s earlier
    ch = next(m for m in s.project.sections if m.name == "Chorus")     # undo restores new objects
    assert abs(section_bounds(s.project, ch, s.duration)[0] - a) < 0.05


def test_follow_glides(app, win):
    s = win.s
    c = win.canvas
    c.set_view(t0=0, pps=100)
    c.follow = True
    eng = s.engine
    eng.seek(1.0)
    eng.play()
    try:
        xs = []
        for _ in range(60):
            pump(app, 0.03)
            xs.append(c._last_playhead_x)
            if c.t0 > 0.5:
                break
        assert c.t0 > 0 or eng.position() < c.visible_seconds * 0.4
        pump(app, 0.3)
        target = c.x_of(eng.position())
        assert abs(target - (HEADER_W + (c.width() - HEADER_W) * c.FOLLOW_AT)) < 40 or c.t0 == 0
    finally:
        eng.pause()


def test_save_with_numpy_values_updates_title(app, win, monkeypatch):
    s = win.s
    from cueforge.core.model import Cue
    s.project.cues.append(Cue(lane_id=s.project.lanes[0].id, time=np.float32(1.5)))
    path = win.tmp / "Show.cueproj"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    assert win.save_as()
    assert win.windowTitle().startswith("Show") and path.exists()


def test_bulk_hold_fade(app, win):
    s = win.s
    a = s.add_cue(s.project.lanes[0].id, 1.0)
    b = s.add_cue(s.project.lanes[1].id, 2.0)
    s.select(cues={a.id, b.id})
    from cueforge.ui.dialogs import HoldFadeDialog
    d = HoldFadeDialog(s, {a.id, b.id}, win)
    d.hold_mode.setCurrentIndex(d.hold_mode.findData("temp"))
    d.hold.setValue(0.75)
    d.fade.setValue(1.5)
    d.accept()
    assert s.project.cue(a.id).duration == 0.75 and s.project.cue(b.id).duration == 0.75
    assert s.project.cue(a.id).fade == 1.5
    # the Hold box changes every selected Temp
    win.hold_spin.setValue(0.4)
    assert s.project.cue(a.id).duration == 0.4 and s.project.cue(b.id).duration == 0.4
    # and back to Go cues in one step
    s.set_hold_fade([a.id, b.id], hold=None)
    assert s.project.cue(a.id).duration is None and s.project.cue(b.id).duration is None
    win._undo()
    assert s.project.cue(a.id).duration == 0.4


def test_tap_uses_key_down_time(app, win):
    """A key press that waited in the queue (UI busy) still lands where it was played."""
    s = win.s
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    s.snap = False
    s.engine.seek(10.0)
    s.engine.play()
    try:
        pump(app, 0.2)
        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_3, Qt.NoModifier, "3")
        win._note_key_down(ev)
        down = win._key_down[Qt.Key_3][0]
        time.sleep(0.3)                                   # the app was busy
        lane = s.project.lanes[2]
        win._tap_press(lane.id, "3")
        win._tap_release("3", Qt.Key_3)
    finally:
        s.engine.pause()
    cue = s.project.cues_in_lane(lane.id)[-1]
    assert abs(cue.time - down) < 0.05 and s.engine.position() - cue.time > 0.25


def test_midi_fighter_spectra(app, win):
    """Spectra: buttons on MIDI channel 3, notes 36-51; LEDs are lit on channel 3."""
    import mido
    from cueforge.control.actions import ControlSettings, _old_default_map
    hub = win.control
    s = win.s
    hub._in_name = "Midi Fighter Spectra"
    s.engine.seek(3.0)
    hub.handle_midi(mido.Message("note_on", channel=2, note=36, velocity=127), 3.0)
    hub.handle_midi(mido.Message("note_off", channel=2, note=36, velocity=0), 3.1)
    wait(app, lambda: len(s.project.cues) == 1, 5)
    assert s.project.cues[0].lane_id == s.project.lanes[0].id
    msgs = hub.feedback_messages()
    assert msgs and all(m.channel == 2 for m in msgs)
    assert hub.feedback_mode() == "midifighter"
    # settings saved with the old channel-1-only defaults are upgraded to "any channel"
    cfg = ControlSettings(midi_map=_old_default_map())
    assert all(m.channel == -1 for m in cfg.mappings())


def test_temp_lengths_snap_and_short_taps_are_even(app, win):
    import mido
    s = win.s
    from cueforge.core.model import BeatGrid
    s.set_grid(BeatGrid.from_tempo(128, 0.0, 60.0, 4))       # beat = 0.469 s
    s.snap = True
    s.set_temp_hold(0.5)
    hub = win.control
    hub.cfg.hold_from_press = True
    beat = 60 / 128
    on_grid = lambda x: abs(x / beat - round(x / beat)) < 0.03 * 2   # within a frame or so
    # quick tap on a Temp pad: the standard length, end on the grid
    hub.handle_midi(mido.Message("note_on", channel=2, note=46, velocity=127), 3.02)
    hub.handle_midi(mido.Message("note_off", channel=2, note=46, velocity=0), 3.10)
    wait(app, lambda: s.project.cues and s.project.cues[-1].duration, 5)
    c = s.project.cues[-1]
    assert on_grid(c.time) and on_grid(c.time + c.duration) and c.duration >= beat - 0.02
    # a long hold keeps its own length, end snapped
    hub.handle_midi(mido.Message("note_on", channel=2, note=46, velocity=127), 10.03)
    hub.handle_midi(mido.Message("note_off", channel=2, note=46, velocity=0), 12.2)
    wait(app, lambda: len(s.project.cues) == 2 and s.project.cues[-1].duration, 5)
    c = sorted(s.project.cues, key=lambda x: x.time)[-1]
    assert on_grid(c.time + c.duration) and 1.8 < c.duration < 2.6
    # ＋Temp (W): the Hold box length, end snapped to the grid
    c3 = s.add_at_playhead(temp=True, lane_id=s.project.lanes[2].id, t=20.0)
    assert on_grid(c3.time + c3.duration)
