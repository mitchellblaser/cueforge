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
from cueforge.ui.timeline import HEADER_W, LANE_H, RULER_H, TRACK_H
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
    return RULER_H + len(w.s.project.tracks) * TRACK_H + idx * LANE_H + LANE_H // 2


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
