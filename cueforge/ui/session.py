"""Session: the open project plus audio, undo, selection and background jobs.

Widgets talk to the Session; the Session mutates the Project through
`core.editing` and emits signals so every view refreshes.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal

from ..analysis.pipeline import AnalysisOptions, AnalysisResult, Cancelled, run_analysis
from ..analysis.tuning import learn_thresholds
from ..audio.engine import AudioEngine, db_to_gain
from ..audio.loader import AudioData, load_audio
from ..core import editing
from ..core.model import (BeatGrid, Lane, LANE_COLORS, Project, Suggestion, TRACK_COLORS, Track)
from ..core.project_io import analysis_cache_dir, load_project, save_project
from ..core.settings import UserSettings
from ..core.undo import UndoStack
from .workers import Job, start_job


# single-key shortcuts that lanes may not use as tap keys
RESERVED_KEYS = set("axsgiolcbfzpt")


def guess_role(filename: str, is_first: bool) -> str:
    n = os.path.basename(filename).lower()
    if "click" in n or "metronome" in n:
        return "Click"
    if any(w in n for w in ("guide", "cue", "count", "vox guide", "spoken")):
        return "Cue/Guide"
    if any(w in n for w in ("stem", "drum", "bass", "vocal", "vox", "keys", "guitar", "synth", "perc")):
        return "Stem"
    return "Track" if is_first else "Other"


class Session(QObject):
    project_replaced = Signal()
    songs_changed = Signal()       # setlist names/order/settings
    song_changed = Signal()        # the current song switched
    song_will_change = Signal()    # about to switch (views store their state)
    tracks_changed = Signal()
    mixer_changed = Signal()
    lanes_changed = Signal()
    cues_changed = Signal()
    selection_changed = Signal()
    dirty_changed = Signal(bool)
    status = Signal(str)
    busy = Signal(str)            # message while a job runs ("" when idle)
    progress = Signal(float, str)
    analysis_done = Signal(object)

    def __init__(self, settings: UserSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or UserSettings()
        self.engine = AudioEngine()
        dev = self.settings.get("output_device")
        if dev is not None:
            self.engine.device = dev
        self.audio: dict[str, AudioData] = {}
        self.load_errors: dict[str, str] = {}
        self.sel_cues: set[str] = set()
        self.sel_sugs: set[str] = set()
        self.snap = bool(self.settings.get("snap", True))
        self._loading: set[str] = set()
        self._analysis_relay = None
        self.project = Project()
        self.undo = UndoStack(self.project)
        self.undo.on_change = self._on_undo_change
        self._was_dirty = False
        self._song_lru: list[str] = []
        self._engine_song: str | None = None
        self._load_current_song()

    # ------------------------------------------------------------------ songs
    AUDIO_CACHE_SONGS = 3   # keep decoded audio for this many recently used songs

    def _load_current_song(self) -> None:
        """Put the current song's tracks into the audio engine (loading if needed)."""
        song = self.project.song
        self.engine.pause()
        for tid in list(self.engine.tracks):
            self.engine.remove_track(tid)
        self._engine_song = song.id
        for t in song.tracks:
            a = self.audio.get(t.id)
            if a is not None:
                self.engine.set_track(t.id, a.samples)
                self.engine.update_track(t.id, gain_db=t.gain_db, mute=t.mute, solo=t.solo, offset=t.offset)
            elif t.id not in self._loading:
                self._load_track_audio(t)
        self.engine.seek(0)
        self.engine.loop = song.loop
        self.engine.loop_enabled = False
        self._sync_engine()
        # drop decoded audio of songs not used recently
        self._song_lru = [song.id] + [x for x in self._song_lru if x != song.id]
        keep = set(self._song_lru[:self.AUDIO_CACHE_SONGS])
        keep_tracks = {t.id for s in self.project.songs if s.id in keep for t in s.tracks}
        for tid in list(self.audio):
            if tid not in keep_tracks:
                del self.audio[tid]

    def switch_song(self, sid: str) -> None:
        if sid == self.project.song.id:
            return
        self.song_will_change.emit()
        self.project.song.loop = self.engine.loop
        if not self.project.select_song(sid):
            return
        self.sel_cues.clear()
        self.sel_sugs.clear()
        self._load_current_song()
        self.song_changed.emit()
        self.project_replaced.emit()
        self.songs_changed.emit()

    def add_song(self, paths: list[str] | None = None, name: str = "") -> str:
        if not name and paths:
            name = os.path.splitext(os.path.basename(paths[0]))[0]
        song = self.project.add_song(name)
        self._touch()
        self.switch_song(song.id)
        if paths:
            self.import_audio(paths)
        self.songs_changed.emit()
        return song.id

    def remove_song(self, sid: str) -> None:
        song = self.project.song_by_id(sid)
        if not song:
            return
        was_current = sid == self.project.song.id
        idx = self.project.songs.index(song)
        for t in song.tracks:
            self.audio.pop(t.id, None)
        self.project.remove_song(sid)
        self._touch()
        if was_current:
            self.project.current = min(idx, len(self.project.songs) - 1)
            self._load_current_song()
            self.song_changed.emit()
            self.project_replaced.emit()
        self.songs_changed.emit()

    def move_song(self, sid: str, direction: int) -> None:
        self.project.move_song(sid, direction)
        self._touch()
        self.songs_changed.emit()

    def reorder_songs(self, ids: list[str]) -> None:
        by_id = {s.id: s for s in self.project.songs}
        if set(ids) != set(by_id):
            return
        cur = self.project.song.id
        self.project.songs = [by_id[i] for i in ids]
        self.project.select_song(cur)
        self._touch()
        self.songs_changed.emit()

    def update_song(self, sid: str, **fields) -> None:
        song = self.project.song_by_id(sid)
        if not song:
            return
        for k, v in fields.items():
            setattr(song, k, v)
        self._touch()
        self.songs_changed.emit()
        if sid == self.project.song.id:
            self.cues_changed.emit()   # timecode display / ruler may change

    def auto_number_setlist(self, first_tc: float = 3600.0, gap: float = 3600.0) -> None:
        """Song N starts at first_tc + (N-1)*gap, uses Timecode slot N and cues N01..."""
        for i, song in enumerate(self.project.songs):
            song.tc_offset = first_tc + i * gap
            song.ma3_timecode = i + 1
            song.cue_start = float(100 * i + 1)
        self._touch()
        self.songs_changed.emit()
        self.cues_changed.emit()

    # ------------------------------------------------------------------ project
    @property
    def duration(self) -> float:
        d = self.engine.duration
        if self.project.cues:
            d = max(d, self.project.cues[-1].time + 2)
        return max(d, 10.0)

    def _replace_project(self, p: Project) -> None:
        self.engine.pause()
        for tid in list(self.engine.tracks):
            self.engine.remove_track(tid)
        self.audio.clear()
        self.load_errors.clear()
        self.project = p
        self.undo = UndoStack(p)
        self.undo.on_change = self._on_undo_change
        self.sel_cues.clear()
        self.sel_sugs.clear()
        self._song_lru = []
        self._engine_song = None
        self.project_replaced.emit()
        self._emit_dirty()
        self._load_current_song()
        self.songs_changed.emit()

    def new_project(self) -> None:
        self._replace_project(Project())

    def open_project(self, path: str) -> None:
        self._replace_project(load_project(path))
        self.settings.add_recent(path)

    def save(self, path: str | None = None) -> None:
        path = path or self.project.path
        self.project.song.loop = self.engine.loop
        save_project(self.project, path)
        self.undo.mark_clean()
        self.settings.add_recent(path)
        self._emit_dirty()
        self.status.emit(f"Saved {path}")

    def is_dirty(self) -> bool:
        return self.undo.is_dirty() or getattr(self, "_mixer_dirty", False)

    def _emit_dirty(self) -> None:
        if not self.undo.is_dirty():
            self._mixer_dirty = False
        self.dirty_changed.emit(self.is_dirty())

    def _touch(self) -> None:
        """Non-undoable change (mixer, settings) that still needs saving."""
        self._mixer_dirty = True
        self.dirty_changed.emit(True)

    def _on_undo_change(self) -> None:
        if self.project.song.id != self._engine_song:   # undo jumped to another song
            self._load_current_song()
            self.song_changed.emit()
            self.project_replaced.emit()
            self.songs_changed.emit()
        # prune selection of things that no longer exist
        ids = {c.id for c in self.project.cues}
        self.sel_cues &= ids
        sids = {s.id for s in self.project.suggestions if s.status == "pending"}
        self.sel_sugs &= sids
        self._sync_engine()
        self.lanes_changed.emit()
        self.cues_changed.emit()
        self.selection_changed.emit()
        self.dirty_changed.emit(self.is_dirty())

    # ------------------------------------------------------------------ tracks
    def import_audio(self, paths: list[str]) -> None:
        first = not any(t.role == "Track" for t in self.project.tracks)
        for i, path in enumerate(paths):
            role = guess_role(path, first and i == 0)
            t = Track(name=os.path.splitext(os.path.basename(path))[0], path=path, role=role,
                      analyse=role in ("Track", "Stem"),
                      color=TRACK_COLORS[len(self.project.tracks) % len(TRACK_COLORS)])
            if role == "Click":
                t.gain_db = -6.0
            self.project.tracks.append(t)
            self._load_track_audio(t)
        self._touch()
        self.tracks_changed.emit()

    def _load_track_audio(self, t: Track) -> None:
        self._loading.add(t.id)
        self.busy.emit(f"Loading {t.name}…")
        job = Job(lambda path, progress=None, cancelled=None: load_audio(path), t.path)
        relay = start_job(job)
        relay.finished.connect(lambda audio, tid=t.id: self._track_loaded(tid, audio))
        relay.failed.connect(lambda err, tid=t.id: self._track_failed(tid, err))

    def _track_loaded(self, tid: str, audio: AudioData) -> None:
        self._loading.discard(tid)
        if not any(t.id == tid for t in self.project.all_tracks()):
            return
        self.audio[tid] = audio
        self.load_errors.pop(tid, None)
        t = self.project.track(tid)      # None if it belongs to another song
        if not t:
            if not self._loading:
                self.busy.emit("")
            return
        self.engine.set_track(tid, audio.samples)
        self.engine.update_track(tid, gain_db=t.gain_db, mute=t.mute, solo=t.solo, offset=t.offset)
        if not self._loading:
            self.busy.emit("")
        self.status.emit(f"Loaded {t.name} ({audio.duration:.1f}s)")
        self.tracks_changed.emit()

    def _track_failed(self, tid: str, err: str) -> None:
        self._loading.discard(tid)
        if not self._loading:
            self.busy.emit("")
        t = self.project.track(tid)
        self.load_errors[tid] = err.splitlines()[0] if err else "failed"
        self.status.emit(f"Could not load {t.name if t else tid}: {self.load_errors[tid]}")
        self.tracks_changed.emit()

    def relink_track(self, tid: str, path: str) -> None:
        t = self.project.track(tid)
        if t:
            t.path = path
            self._touch()
            self._load_track_audio(t)

    def remove_track(self, tid: str) -> None:
        self.project.tracks = [t for t in self.project.tracks if t.id != tid]
        self.audio.pop(tid, None)
        self.engine.remove_track(tid)
        self._touch()
        self.tracks_changed.emit()

    def update_track(self, tid: str, **fields) -> None:
        t = self.project.track(tid)
        if not t:
            return
        for k, v in fields.items():
            setattr(t, k, v)
        self.engine.update_track(tid, gain_db=t.gain_db, mute=t.mute, solo=t.solo, offset=t.offset)
        self._touch()
        if set(fields) - {"gain_db", "mute", "solo"}:
            self.tracks_changed.emit()
        else:
            self.mixer_changed.emit()

    def move_track(self, tid: str, direction: int) -> None:
        tr = self.project.tracks
        i = next((k for k, t in enumerate(tr) if t.id == tid), -1)
        j = i + direction
        if 0 <= i < len(tr) and 0 <= j < len(tr):
            tr[i], tr[j] = tr[j], tr[i]
            self._touch()
            self.tracks_changed.emit()

    def update_mixer(self, **fields) -> None:
        for k, v in fields.items():
            setattr(self.project.mixer, k, v)
        self._sync_engine()
        self._touch()
        self.mixer_changed.emit()

    def _sync_engine(self) -> None:
        m = self.project.mixer
        g = self.project.beat_grid
        self.engine.master_gain = db_to_gain(m.master_db)
        self.engine.set_click(g.beats, g.downbeats, m.click_enabled, m.click_db)
        self.engine.set_blips([c.time for c in self.project.cues], m.blips_enabled, m.blips_db)

    # ------------------------------------------------------------------ edits
    @contextmanager
    def edit(self, label: str):
        self.undo.push(label)
        yield
        self.project.sort_cues()
        self._sync_engine()
        self.cues_changed.emit()
        self.selection_changed.emit()
        self.dirty_changed.emit(self.is_dirty())

    def lane_for_key(self, key: str) -> Lane | None:
        return next((l for l in self.project.lanes if l.tap_key and l.tap_key.lower() == key.lower()), None)

    def add_cue(self, lane_id: str, t: float, snap: bool | None = None, label: str = ""):
        snap = self.snap if snap is None else snap
        with self.edit("Add cue"):
            c = editing.add_cue(self.project, lane_id, t, snap, label)
            self.sel_cues = {c.id}
            self.sel_sugs.clear()
        return c

    def tap(self, lane_id: str) -> None:
        """Tap a cue at the current (heard) playback position. Live taps are not
        grid-snapped unless snapping is on, and always land on a frame."""
        t = self.engine.position()
        with self.edit("Tap cue"):
            c = editing.add_cue(self.project, lane_id, t, self.snap)
            self.sel_cues = {c.id}

    def delete_selected(self) -> None:
        if self.sel_cues:
            with self.edit("Delete cues"):
                editing.delete_cues(self.project, set(self.sel_cues))
                self.sel_cues.clear()
        elif self.sel_sugs:
            self.reject(self.sel_sugs)

    def move_selected(self, delta: float) -> None:
        if self.sel_cues and delta:
            with self.edit("Move cues"):
                editing.move_cues(self.project, set(self.sel_cues), delta)

    def set_cue_times(self, times: dict[str, float], label: str = "Move cues") -> None:
        with self.edit(label):
            for c in self.project.cues:
                if c.id in times:
                    c.time = editing.snap_time(self.project, times[c.id], False)

    def nudge(self, frames: int = 0, beats: int = 0) -> None:
        if not self.sel_cues:
            return
        with self.edit("Nudge"):
            if beats:
                editing.nudge_beats(self.project, set(self.sel_cues), beats)
            else:
                editing.nudge_frames(self.project, set(self.sel_cues), frames)

    def snap_selected(self) -> None:
        if self.sel_cues:
            with self.edit("Snap to grid"):
                editing.snap_cues_to_grid(self.project, set(self.sel_cues))

    def move_selected_to_lane(self, lane_id: str) -> None:
        if self.sel_cues:
            with self.edit("Move to lane"):
                editing.move_cues_to_lane(self.project, set(self.sel_cues), lane_id)

    def update_cue(self, cue_id: str, **fields) -> None:
        c = self.project.cue(cue_id)
        if not c:
            return
        with self.edit("Edit cue"):
            for k, v in fields.items():
                setattr(c, k, v)
            if "time" in fields:
                c.time = editing.snap_time(self.project, c.time, False)
            if c.duration is not None and c.duration <= 0:
                c.duration = None

    def renumber_lane(self, lane_id: str, start: float, step: float) -> None:
        with self.edit("Renumber"):
            editing.renumber_lane(self.project, lane_id, start, step)

    # lanes
    def add_lane(self, name: str = "") -> Lane:
        with self.edit("Add lane"):
            n = len(self.project.lanes)
            used_keys = {l.tap_key for l in self.project.lanes}
            key = next((str(k) for k in range(1, 10) if str(k) not in used_keys), "")
            lane = Lane(name or f"Lane {n + 1}", LANE_COLORS[n % len(LANE_COLORS)], key,
                        max((l.ma3_sequence for l in self.project.lanes), default=0) + 1)
            self.project.lanes.append(lane)
        self.lanes_changed.emit()
        return lane

    def update_lane(self, lane_id: str, **fields) -> None:
        lane = self.project.lane(lane_id)
        if not lane:
            return
        with self.edit("Edit lane"):
            for k, v in fields.items():
                setattr(lane, k, v)
        self.lanes_changed.emit()

    def remove_lane(self, lane_id: str) -> None:
        if len(self.project.lanes) <= 1:
            return
        with self.edit("Remove lane"):
            self.project.lanes = [l for l in self.project.lanes if l.id != lane_id]
            gone = {c.id for c in self.project.cues if c.lane_id == lane_id}
            editing.delete_cues(self.project, gone)
            for s in self.project.suggestions:
                if s.lane_id == lane_id:
                    s.lane_id = ""
            for k, v in list(self.project.analysis.lane_for_kind.items()):
                if v == lane_id:
                    self.project.analysis.lane_for_kind[k] = self.project.lanes[0].id
            for s in self.project.suggestions:
                if not s.lane_id:
                    s.lane_id = self.project.lane_for_kind(s.kind)
        self.lanes_changed.emit()

    def move_lane(self, lane_id: str, direction: int) -> None:
        ls = self.project.lanes
        i = next((k for k, l in enumerate(ls) if l.id == lane_id), -1)
        j = i + direction
        if 0 <= i < len(ls) and 0 <= j < len(ls):
            with self.edit("Reorder lanes"):
                ls[i], ls[j] = ls[j], ls[i]
            self.lanes_changed.emit()

    def set_kind_lane(self, kind: str, lane_id: str) -> None:
        with self.edit("Suggestion lane"):
            self.project.analysis.lane_for_kind[kind] = lane_id
            for s in self.project.suggestions:
                if s.kind == kind and s.status == "pending":
                    s.lane_id = lane_id

    # ------------------------------------------------------------------ selection
    def select(self, cues: set[str] | None = None, sugs: set[str] | None = None, add: bool = False) -> None:
        if not add:
            self.sel_cues = set(cues or ())
            self.sel_sugs = set(sugs or ())
        else:
            self.sel_cues ^= set(cues or ())
            self.sel_sugs ^= set(sugs or ())
        self.selection_changed.emit()

    # ------------------------------------------------------------------ suggestions
    def _record(self, sugs: list[Suggestion], accepted: bool) -> None:
        hist = self.settings.get("decisions", [])
        for s in sugs:
            hist.append({"kind": s.kind, "confidence": s.confidence, "accepted": accepted, "label": s.label})
        self.settings.set("decisions", hist[-5000:])

    def accept(self, ids) -> int:
        sugs = [s for s in self.project.suggestions if s.id in set(ids) and s.status == "pending"]
        if not sugs:
            return 0
        with self.edit(f"Accept {len(sugs)} suggestion(s)"):
            n = editing.accept_many(self.project, sugs)
            self.sel_sugs -= {s.id for s in sugs}
            self.sel_cues = {s.cue_id for s in sugs if s.cue_id}
        self._record(sugs, True)
        self.status.emit(f"Accepted {n} suggestion(s)")
        return n

    def accept_as_steps(self, sid: str, lane_id: str | None = None) -> int:
        """Accept a lead-line suggestion as one cue per note (chase steps)."""
        sg = self.project.suggestion(sid)
        if not sg or sg.status != "pending" or not sg.steps:
            return 0
        with self.edit(f"Accept {len(sg.steps)} chase steps"):
            cues = editing.accept_as_steps(self.project, sg, lane_id)
            self.sel_sugs.discard(sid)
            self.sel_cues = {c.id for c in cues}
        self._record([sg], True)
        self.status.emit(f"Added {len(cues)} chase steps")
        return len(cues)

    def reject(self, ids) -> int:
        sugs = [s for s in self.project.suggestions if s.id in set(ids) and s.status == "pending"]
        if not sugs:
            return 0
        with self.edit(f"Reject {len(sugs)} suggestion(s)"):
            n = editing.reject_many(self.project, sugs)
            self.sel_sugs -= {s.id for s in sugs}
        self._record(sugs, False)
        self.status.emit(f"Rejected {n} suggestion(s)")
        return n

    def visible_ids(self, kind: str | None = None, t0: float | None = None, t1: float | None = None) -> list[str]:
        return [s.id for s in self.project.visible_suggestions()
                if (kind is None or s.kind == kind)
                and (t0 is None or s.time >= t0) and (t1 is None or s.time <= t1)]

    def clear_pending(self, kind: str | None = None) -> None:
        with self.edit("Clear suggestions"):
            self.project.suggestions = [s for s in self.project.suggestions
                                        if s.status != "pending" or (kind and s.kind != kind)]
            self.sel_sugs.clear()

    def set_filter(self, kind: str, threshold: float | None = None, visible: bool | None = None) -> None:
        if threshold is not None:
            self.project.analysis.thresholds[kind] = threshold
        if visible is not None:
            self.project.analysis.visible[kind] = visible
        vis = {s.id for s in self.project.visible_suggestions()}
        self.sel_sugs &= vis
        self._touch()
        self.cues_changed.emit()

    def learned_thresholds(self) -> dict:
        return learn_thresholds(self.settings.get("decisions", []))

    # ------------------------------------------------------------------ grid
    def set_grid(self, grid: BeatGrid, label: str = "Set beat grid") -> None:
        with self.edit(label):
            self.project.beat_grid = grid

    def accept_grid(self) -> None:
        g = self.project.beat_grid
        if g.beats and not g.confirmed:
            with self.edit("Accept beat grid"):
                g.confirmed = True

    def grid_from_tempo(self, bpm: float, first_downbeat: float, bpb: int) -> None:
        self.set_grid(BeatGrid.from_tempo(bpm, first_downbeat, self.duration + 5, bpb), "Set tempo")

    def set_downbeat_at(self, t: float) -> None:
        """Re-phase the grid so the beat nearest t becomes beat 1."""
        g = self.project.beat_grid
        if not g.beats:
            return
        import numpy as np
        b = np.asarray(g.beats)
        j = int(np.argmin(np.abs(b - t)))
        bpb = g.beats_per_bar
        downs = [float(x) for k, x in enumerate(b) if (k - j) % bpb == 0]
        with self.edit("Set downbeat"):
            self.project.beat_grid = BeatGrid(list(g.beats), downs, bpb, g.source, g.confirmed, g.confidence)

    def shift_grid(self, delta: float) -> None:
        g = self.project.beat_grid
        if g.beats:
            with self.edit("Shift grid"):
                self.project.beat_grid = g.shifted(delta)

    def halve_tempo(self) -> None:
        from ..core.grid_tools import halve_tempo
        if self.project.beat_grid.beats:
            self.set_grid(halve_tempo(self.project.beat_grid), "Halve tempo")

    def double_tempo(self) -> None:
        from ..core.grid_tools import double_tempo
        if self.project.beat_grid.beats:
            self.set_grid(double_tempo(self.project.beat_grid), "Double tempo")

    # live tap-along grid -------------------------------------------------
    def start_tap_grid(self) -> None:
        self.grid_taps: list[float] = []
        self.tap_grid_active = True
        self.status.emit("Tap-along grid: press T on every beat while playing (first tap = bar 1). "
                         "Turn tap mode off to build the grid.")

    def tap_grid_beat(self) -> None:
        if getattr(self, "tap_grid_active", False) and self.engine.playing:
            self.grid_taps.append(self.engine.position())
            self.status.emit(f"Tap-along grid: {len(self.grid_taps)} taps")

    def finish_tap_grid(self) -> bool:
        from ..core.grid_tools import grid_from_taps, merge_grid
        self.tap_grid_active = False
        taps = sorted(getattr(self, "grid_taps", []))
        self.grid_taps = []
        if len(taps) < 4:
            self.status.emit("Tap-along grid: need at least 4 taps")
            return False
        onsets = self._onsets_between(taps[0] - 0.5, taps[-1] + 0.5)
        bpb = self.project.beat_grid.beats_per_bar or 4
        tapped = grid_from_taps(taps, onsets, bpb)
        self.set_grid(merge_grid(self.project.beat_grid, tapped), "Tap-along grid")
        self.status.emit(f"Grid built from {len(taps)} taps ({tapped.bpm():.1f} BPM), snapped to the drums")
        return True

    def _onsets_between(self, t0: float, t1: float):
        """Drum hit times of the analysed audio in [t0, t1] (for snapping taps)."""
        import numpy as np
        try:
            from ..audio.loader import resample
            sr = 22050
            mix = None
            for t in self.project.tracks:
                a = self.audio.get(t.id)
                if a is None or t.role not in ("Track", "Stem"):
                    continue
                i0 = max(0, int((t0 - t.offset) * a.sr))
                i1 = max(i0, int((t1 - t.offset) * a.sr))
                seg = a.samples[i0:i1].mean(axis=1)
                start = max(t0, t.offset)
                if mix is None:
                    mix = (start, seg, a.sr)
                elif len(seg) == len(mix[1]):
                    mix = (mix[0], mix[1] + seg, a.sr)
            if mix is None or len(mix[1]) < 2048:
                return np.zeros(0)
            start, seg, src_sr = mix
            y = resample(seg[:, None].astype(np.float32), src_sr, sr)[:, 0]
            # lock taps to the drums: kick / snare / crash hits, not every onset
            from ..analysis.hits import detect_hits
            hits = detect_hits(y, sr)
            return np.asarray(sorted(h.time for h in hits if h.confidence >= 0.3)) + start
        except Exception:
            return np.zeros(0)

    def clear_grid(self) -> None:
        with self.edit("Clear grid"):
            self.project.beat_grid = BeatGrid()

    # ------------------------------------------------------------------ analysis
    def analysis_running(self) -> bool:
        return self._analysis_relay is not None

    def run_analysis(self, opts: AnalysisOptions) -> bool:
        if self._analysis_relay is not None or self._loading:
            return False
        ids = {t.id for t in self.project.tracks}
        audio = {k: v for k, v in self.audio.items() if k in ids}
        if not audio:
            self.status.emit("Import audio first")
            return False
        cache = None
        try:
            cache = analysis_cache_dir(self.project)
        except OSError:
            pass
        import copy
        snapshot = copy.copy(self.project)   # pins the current song even if the user switches
        self._analysis_song = self.project.song.id
        job = Job(run_analysis, snapshot, audio, opts, cache_dir=cache)
        self.busy.emit("Analysing…")
        relay = start_job(job)
        relay.progress.connect(self.progress.emit)
        relay.finished.connect(self._analysis_finished)
        relay.failed.connect(self._analysis_failed)
        self._analysis_relay = relay
        return True

    def cancel_analysis(self) -> None:
        if self._analysis_relay is not None:
            self._analysis_relay.job.cancel_requested = True

    def _analysis_finished(self, res: AnalysisResult) -> None:
        self._analysis_relay = None
        self.busy.emit("")
        origin = getattr(self, "_analysis_song", self.project.song.id)
        if not self.project.song_by_id(origin):
            self.status.emit("Analysis finished, but its song was removed")
            return
        current = self.project.song.id
        self.project.select_song(origin)   # apply results to the song that was analysed
        try:
            self._apply_analysis(res)
        finally:
            self.project.select_song(current)
            self._sync_engine()
            self.cues_changed.emit()
        if origin != current:
            name = self.project.song_by_id(origin).name
            res.log.append(f"(results added to '{name}')")
        self.lanes_changed.emit()
        self.songs_changed.emit()
        self.status.emit(res.log[-1] if res.log else "Analysis done")
        self.analysis_done.emit(res)

    def _apply_analysis(self, res: AnalysisResult) -> None:
        with self.edit("Analyse"):
            if res.grid and not res.grid.empty:
                cur = self.project.beat_grid
                if not cur.confirmed:
                    self.project.beat_grid = res.grid
                else:
                    res.log.append("Kept your confirmed beat grid (clear it to use the detected one).")
            n_lanes = len(self.project.lanes)
            added = editing.merge_suggestions(self.project, res.suggestions, res.kinds)
            res.log.append(f"{added} new suggestion(s) added")
            if len(self.project.lanes) > n_lanes:
                res.log.append("Added lanes for the new suggestion types: " +
                               ", ".join(l.name for l in self.project.lanes[n_lanes:]))

    def _analysis_failed(self, err: str) -> None:
        self._analysis_relay = None
        self.busy.emit("")
        if "Cancelled" in err.splitlines()[-1] if err else False:
            self.status.emit("Analysis cancelled")
            return
        self.status.emit("Analysis failed")
        self.analysis_done.emit(err)
