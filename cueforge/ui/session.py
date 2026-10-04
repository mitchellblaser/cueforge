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
from ..core.timecode import snap_to_frame
from ..core.undo import UndoStack
from .workers import Job, start_job


# single-key shortcuts that lanes may not use as tap keys
RESERVED_KEYS = set("axsgiolcbfzptqwmd")


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
    active_lane_changed = Signal()
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
    grid_rephased = Signal()
    auto_advance_changed = Signal()      # bar 1 / downbeats changed by hand (offer re-analysis)

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

    def add_songs(self, groups: list[tuple[str, list[str]]]) -> list[str]:
        """Bulk import: one new song per (name, audio files). Audio loads when a song is
        opened (or is read by the analysis), so a whole setlist imports instantly. An empty
        "Song 1" in a fresh project is reused for the first song."""
        p = self.project
        ids = []
        for gi, (name, paths) in enumerate(groups):
            if not paths:
                continue
            reuse = gi == 0 and len(p.songs) == 1 and not p.song.tracks and not p.song.cues
            song = p.song if reuse else p.add_song(name)
            if reuse:
                song.name = name
            ordered = sorted(paths, key=lambda x: os.path.basename(x).lower())
            roles = ["Track" if len(ordered) == 1 else guess_role(x, False) for x in ordered]
            if "Track" not in roles and "Other" in roles:
                roles[roles.index("Other")] = "Track"        # first plain file of a folder = the mix
            for path, role in zip(ordered, roles):
                song.tracks.append(Track(name=os.path.splitext(os.path.basename(path))[0], path=path, role=role,
                                         analyse=role in ("Track", "Stem"),
                                         color=TRACK_COLORS[len(song.tracks) % len(TRACK_COLORS)],
                                         gain_db=-6.0 if role == "Click" else 0.0))
            if reuse:
                self._load_current_song()
            ids.append(song.id)
        self._touch()
        self.songs_changed.emit()
        self.tracks_changed.emit()
        return ids

    def import_cuepoints(self, items, opts) -> dict:
        """Import CuePoints / spreadsheet rows; each song's part is its own undo step."""
        from ..export.cuepoints_import import add_rows, group_by_song, target_song
        p = self.project
        current = p.song.id
        total = {"cues": 0, "songs_created": [], "lanes_created": [], "skipped": 0}
        try:
            for name, group in group_by_song(items, opts).items():
                p.select_song(current)      # rows without a Track go to the song that was open
                song, created = target_song(p, name, group, opts)
                if created:
                    total["songs_created"].append(song.name)
                p.select_song(song.id)
                with self.edit(f"Import CuePoints ({song.name})"):
                    r = add_rows(p, group, opts)
                for k in ("cues", "skipped"):
                    total[k] += r[k]
                total["lanes_created"] += r["lanes_created"]
        finally:
            p.select_song(current)
            self._sync_engine()
            self._touch()
            self.lanes_changed.emit()
            self.songs_changed.emit()
            self.cues_changed.emit()
        return total

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
        self.cancel_analysis()                 # its results belong to the old project
        self.engine.pause()
        for tid in list(self.engine.tracks):
            self.engine.remove_track(tid)
        self.audio.clear()
        self.load_errors.clear()
        self.project = p
        clashes = [l for l in p.lanes if l.tap_key and l.tap_key.strip().lower() in RESERVED_KEYS]
        for l in clashes:                      # e.g. "D" is now Set bar 1
            l.tap_key = ""
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
        if clashes:
            self.status.emit("Tap key cleared on " + ", ".join(l.name for l in clashes)
                             + ": that key is now a CueForge shortcut (pick another in Lanes)")

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

    # ------------------------------------------------------------------ active lane
    @property
    def active_lane_id(self) -> str:
        lid = getattr(self, "_active_lane", "")
        if not self.project.lane(lid):
            lid = self.project.lanes[0].id if self.project.lanes else ""
            self._active_lane = lid
        return lid

    def set_active_lane(self, lane_id: str) -> None:
        if self.project.lane(lane_id) and lane_id != getattr(self, "_active_lane", ""):
            self._active_lane = lane_id
            self.active_lane_changed.emit()

    def step_active_lane(self, d: int) -> None:
        ids = [l.id for l in self.project.lanes]
        if ids:
            i = ids.index(self.active_lane_id) if self.active_lane_id in ids else 0
            self.set_active_lane(ids[max(0, min(len(ids) - 1, i + d))])

    @property
    def temp_hold(self) -> float:
        return float(self.settings.get("temp_hold", 0.5))

    def set_temp_hold(self, seconds: float) -> None:
        self.settings.set("temp_hold", round(max(0.0, seconds), 3))

    def add_at_playhead(self, temp: bool = False, lane_id: str | None = None, t: float | None = None):
        """Drop a cue (or a Temp with the current hold time) into the active lane at the
        playhead — the heard position while playing (or at time t, e.g. a MIDI hit)."""
        lane_id = lane_id or self.active_lane_id
        if not lane_id:
            return None
        t = self.engine.position() if t is None else t
        with self.edit("Add temp" if temp else "Add cue"):
            c = editing.add_cue(self.project, lane_id, t, self.snap)
            if temp:
                c.duration = self.temp_hold if self.temp_hold > 0 else None
            self.sel_cues = {c.id}
            self.sel_sugs.clear()
        return c

    def set_selected_temp(self, temp: bool) -> None:
        """Turn the selected cues into Temps (with the current hold) or back into cues."""
        if not self.sel_cues:
            return
        with self.edit("Make temp" if temp else "Make cue"):
            for c in self.project.cues:
                if c.id in self.sel_cues:
                    c.duration = (c.duration or self.temp_hold) if temp else None

    # ------------------------------------------------------------------ sections
    sel_section: str = ""

    def song_end(self) -> float:
        return self.duration

    def select_section(self, sid: str) -> None:
        self.sel_section = sid
        self.selection_changed.emit()

    def add_section_at(self, t: float | None = None, name: str = "") -> str:
        from ..core import arrange
        t = self.engine.position() if t is None else t
        with self.edit("Add section"):
            m = arrange.add_section(self.project, t, name)
            arrange.recolor_by_kind(self.project)
        self.sel_section = m.id
        return m.id

    def rename_section(self, sid: str, name: str, all_of_kind: bool = False) -> None:
        from ..core import arrange
        m = next((x for x in self.project.sections if x.id == sid), None)
        if not m or not name.strip():
            return
        with self.edit("Rename section"):
            if all_of_kind:
                kind = m.kind
                same = [x for x in sorted(self.project.sections, key=lambda x: x.time) if x.kind == kind]
                base = name.strip()
                import re
                base = re.sub(r"[\s#]*\d+$", "", base).strip() or base
                for k, x in enumerate(same, 1):
                    x.name = f"{base} {k}" if len(same) > 1 else base
            else:
                m.name = name.strip()
            arrange.recolor_by_kind(self.project)

    def move_section(self, sid: str, t: float) -> None:
        m = next((x for x in self.project.sections if x.id == sid), None)
        if m:
            with self.edit("Move section"):
                m.time = editing.snap_time(self.project, max(0.0, t), self.snap)
                self.project.sections.sort(key=lambda x: x.time)

    def set_section_bounds(self, bounds: dict[str, tuple[float, float | None]], label: str = "Move section") -> None:
        with self.edit(label):
            for m in self.project.sections:
                if m.id in bounds:
                    t, e = bounds[m.id]
                    m.time = snap_to_frame(max(0.0, t), self.project.frame_rate)
                    m.end = None if e is None else snap_to_frame(e, self.project.frame_rate)
            self.project.sections.sort(key=lambda x: x.time)

    def delete_section(self, sid: str) -> None:
        with self.edit("Delete section"):
            self.project.sections = [x for x in self.project.sections if x.id != sid]
        if self.sel_section == sid:
            self.sel_section = ""

    def sections_from_ai(self) -> int:
        from ..core import arrange
        with self.edit("Sections from AI"):
            arrange.sections_from_suggestions(self.project)
        return len(self.project.sections)

    def section_range(self, sid: str) -> tuple[float, float] | None:
        from ..core import arrange
        m = next((x for x in self.project.sections if x.id == sid), None)
        return arrange.section_bounds(self.project, m, self.song_end()) if m else None

    def copy_section_to_repeats(self, sid: str, targets: list[str] | None = None, replace: bool = False,
                                lanes: set[str] | None = None) -> int:
        from ..core import arrange
        m = next((x for x in self.project.sections if x.id == sid), None)
        if not m:
            return 0
        tg = [x for x in self.project.sections if x.id in targets] if targets is not None \
            else arrange.repeats_of(self.project, m)
        if not tg:
            self.status.emit(f"No other '{m.kind}' sections — name repeats the same (e.g. Chorus 1, Chorus 2)")
            return 0
        with self.edit("Copy section cues"):
            n = arrange.copy_section_to(self.project, m, tg, self.song_end(), lanes, replace)
        self.status.emit(f"Copied '{m.name}' to {len(tg)} section(s): {n} cues")
        return n

    def select_section_cues(self, sid: str) -> None:
        r = self.section_range(sid)
        if r:
            self.select(cues={c.id for c in self.project.cues if r[0] - 0.02 <= c.time < r[1] - 0.02})

    # ------------------------------------------------------------------ clipboard
    clipboard = None

    def copy_selected(self) -> int:
        from ..core import arrange
        if not self.sel_cues:
            return 0
        self.clipboard = arrange.copy_cues(self.project, set(self.sel_cues))
        self.status.emit(f"Copied {len(self.clipboard.cues)} cue(s)")
        return len(self.clipboard.cues)

    def cut_selected(self) -> int:
        n = self.copy_selected()
        if n:
            with self.edit("Cut"):
                editing.delete_cues(self.project, set(self.sel_cues))
                self.sel_cues.clear()
        return n

    def paste_at(self, t: float | None = None, lane_override: str | None = None) -> int:
        from ..core import arrange
        if not self.clipboard or self.clipboard.empty:
            self.status.emit("Clipboard is empty")
            return 0
        t = self.engine.position() if t is None else t
        if self.snap:
            t = editing.snap_time(self.project, t, True)
        with self.edit("Paste"):
            made = arrange.paste(self.project, self.clipboard, t, lane_override=lane_override)
            self.sel_cues = {c.id for c in made}
        self.status.emit(f"Pasted {len(made)} cue(s)")
        return len(made)

    def paste_into_section(self, sid: str | None = None) -> int:
        """Paste keeping the copied cues' position relative to their section start."""
        from ..core import arrange
        if not self.clipboard or self.clipboard.empty:
            self.status.emit("Clipboard is empty")
            return 0
        m = next((x for x in self.project.sections if x.id == sid), None) if sid else \
            arrange.section_at(self.project, self.engine.position())
        if not m:
            self.status.emit("No section here — add section markers first (M)")
            return 0
        anchor = arrange.section_anchor(self.project, m, self.clipboard)
        end = self.section_range(m.id)[1]
        with self.edit("Paste into section"):
            made = arrange.paste(self.project, self.clipboard, anchor, end=end)
            self.sel_cues = {c.id for c in made}
        self.status.emit(f"Pasted {len(made)} cue(s) into '{m.name}'")
        return len(made)

    def pattern_fill(self, lane_id: str, t0: float, t1: float, step, offset_beats: float = 0.0,
                     temp: bool = False, replace: bool = False, label: str = "") -> int:
        from ..core import arrange
        with self.edit("Pattern fill"):
            made = arrange.pattern_fill(self.project, lane_id, t0, t1, step, offset_beats,
                                        self.temp_hold if temp else None, replace, label)
            self.sel_cues = {c.id for c in made}
        self.status.emit(f"Pattern fill: {len(made)} cue(s)")
        return len(made)

    def tap(self, lane_id: str) -> None:
        """Tap a cue at the current (heard) playback position. Live taps are not
        grid-snapped unless snapping is on, and always land on a frame."""
        t = self.engine.position()
        with self.edit("Tap cue"):
            c = editing.add_cue(self.project, lane_id, t, self.snap)
            self.sel_cues = {c.id}
        return c

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
        ids = [l.id for l in self.project.lanes]
        i = ids.index(lane_id) if lane_id in ids else -1
        j = i + direction
        if 0 <= i < len(ids) and 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            self.reorder_lanes(ids)

    def move_lane_to(self, lane_id: str, index: int) -> None:
        ids = [l.id for l in self.project.lanes if l.id != lane_id]
        if lane_id not in {l.id for l in self.project.lanes}:
            return
        ids.insert(max(0, min(len(ids), index)), lane_id)
        self.reorder_lanes(ids)

    def reorder_lanes(self, ids: list[str]) -> None:
        """Put the lanes in this order. Digit tap keys follow the position (lane 1 = key 1 …
        lane 9 = key 9); lanes with a letter key keep it."""
        by_id = {l.id: l for l in self.project.lanes}
        order = [by_id[i] for i in ids if i in by_id] + [l for l in self.project.lanes if l.id not in ids]
        if [l.id for l in order] == [l.id for l in self.project.lanes]:
            return
        with self.edit("Reorder lanes"):
            self.project.lanes = order
            for k, lane in enumerate(order):
                if lane.tap_key.isdigit():        # digit keys follow the position; letters / none stay
                    lane.tap_key = str(k + 1) if k < 9 else ""
        self.lanes_changed.emit()

    def auto_number_cues(self, lane_ids: list[str] | None = None) -> int:
        """Clear fixed cue numbers so the cues are numbered automatically again (in time
        order, from the song's first cue number). Returns how many cues changed."""
        lanes = set(lane_ids) if lane_ids else {l.id for l in self.project.lanes}
        cues = [c for c in self.project.cues if c.lane_id in lanes and c.number is not None]
        if not cues:
            return 0
        with self.edit("Number cues automatically"):
            # the live link keeps its record of the old numbers (so it can delete them on the
            # console when allowed) but must not pin these cues back to them
            unpin = self.project.console.setdefault("unpin", [])
            for c in cues:
                c.number = None
                if c.id in self.project.console.get("cues", {}) and c.id not in unpin:
                    unpin.append(c.id)
        return len(cues)

    def number_sequences(self) -> None:
        """MA3 sequences 1, 2, 3 … in lane order (exported lanes first)."""
        lanes = [l for l in self.project.lanes if l.export] + [l for l in self.project.lanes if not l.export]
        with self.edit("Number sequences"):
            for k, l in enumerate(lanes, 1):
                l.ma3_sequence = k
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

    def set_downbeat_at(self, t: float, drop_before: bool = False) -> None:
        """Make the beat nearest t the downbeat of bar 1 (bars re-phased and numbered
        from it). The grid counts as confirmed afterwards, so re-analysis keeps it."""
        from ..core.grid_tools import set_bar_one
        g = self.project.beat_grid
        if not g.beats:
            return
        with self.edit("Set bar 1"):
            self.project.beat_grid = set_bar_one(g, t, drop_before)
        self.grid_rephased.emit()

    def move_bar_one(self, beats: int) -> None:
        from ..core.grid_tools import move_bar_one
        g = self.project.beat_grid
        if not g.beats:
            return
        with self.edit("Move bar 1"):
            self.project.beat_grid = move_bar_one(g, beats)
        self.grid_rephased.emit()

    def drop_beats_before_bar_one(self) -> None:
        from ..core.grid_tools import drop_beats_before_bar_one
        g = self.project.beat_grid
        if not g.downbeats:
            return
        with self.edit("Remove beats before bar 1"):
            self.project.beat_grid = drop_beats_before_bar_one(g)
        self.grid_rephased.emit()

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
    @property
    def auto_advance(self) -> bool:
        """Jump to the next suggestion after accepting / rejecting one."""
        return bool(self.settings.get("auto_advance", True))

    def set_auto_advance(self, on: bool) -> None:
        self.settings.set("auto_advance", bool(on))
        self.auto_advance_changed.emit()

    def analysis_running(self) -> bool:
        return self._analysis_relay is not None or bool(getattr(self, "_queue_active", False))

    def song_minutes(self, song) -> float:
        """Length of a song's analysed audio in minutes (from loaded audio or file headers)."""
        best = 0.0
        for t in song.tracks:
            if not (t.analyse and t.role in ("Track", "Stem")):
                continue
            a = self.audio.get(t.id)
            dur = a.duration if a is not None else 0.0
            if not dur:
                try:
                    import soundfile as sf
                    dur = float(sf.info(t.path).duration)
                except Exception:
                    dur = 0.0
            best = max(best, dur + t.offset)
        return best / 60.0

    def analysable(self, song) -> bool:
        return any(t.analyse and t.role in ("Track", "Stem") and t.path for t in song.tracks)

    def run_analysis(self, opts: AnalysisOptions, song_ids: list[str] | None = None) -> bool:
        """Analyse the current song, or every song in `song_ids` one after another, each in
        a background process (the UI stays responsive; set it going and leave it)."""
        if self.analysis_running():
            return False
        ids = [sid for sid in (song_ids or [self.project.song.id]) if self.project.song_by_id(sid)]
        ids = [sid for sid in ids if self.analysable(self.project.song_by_id(sid))]
        if not ids:
            self.status.emit("Import audio first (a track with role Track or Stem)")
            return False
        self._queue = list(ids)
        self._queue_total = len(ids)
        self._queue_opts = opts
        self._queue_log: list[str] = []
        self._queue_errors: list[str] = []
        self._queue_last = None
        self._queue_ok = 0
        self._queue_active = True
        self.busy.emit("Analysing…")
        self._start_next()
        return True

    def _start_next(self) -> None:
        if not self._queue:
            self._queue_active = False
            self._runner = None
            self.busy.emit("")
            n = self._queue_total
            if n == 1 and self._queue_last is not None:
                self.analysis_done.emit(self._queue_last)
            elif n == 1 and self._queue_errors:
                self.analysis_done.emit(self._queue_errors[0])
            elif n > 1:
                res = AnalysisResult(log=self._queue_log + [f"Analysed {self._queue_ok} of {n} songs"])
                if self._queue_errors:
                    res.log.append(f"{len(self._queue_errors)} failed — see the log above")
                self.analysis_done.emit(res)
            return
        sid = self._queue.pop(0)
        self._analysis_song = sid
        song = self.project.song_by_id(sid)
        if song is None:
            self._start_next()
            return
        import tempfile
        from ..analysis.worker import write_job
        from ..addons import status as ai_status
        from .analysis_runner import AnalysisRunner, ProgressModel
        opts = self._queue_opts
        cache = None
        try:
            cache = analysis_cache_dir(self.project)
        except OSError:
            pass
        try:
            folder = tempfile.mkdtemp(prefix="cueforge-analysis-")
            write_job(folder, self.project, sid, opts, cache)
        except Exception as exc:
            self._job_failed(sid, f"Could not prepare the analysis: {exc}")
            return
        st = ai_status()
        has_stems = any(t.role == "Stem" for t in song.tracks)
        demucs = bool(opts.use_demucs and not has_stems and st.get("demucs"))
        melodic = any(t.role == "Stem" and not any(w in t.name.lower() for w in ("drum", "kick", "snare", "perc", "bass"))
                      for t in song.tracks)
        skip = {p for p, on in ((0.08, opts.grid and not song.beat_grid.confirmed), (0.25, opts.fills),
                                (0.35, opts.hits), (0.45, opts.harmony),
                                (0.55, opts.melody and (melodic or demucs or opts.melody_from_mix)),
                                (0.60, opts.sections), (0.80, opts.energy)) if not on}
        model = ProgressModel(self.song_minutes(song), bool(opts.use_deep_models and st.get("beat_this")),
                              demucs, self.settings.get("analysis_timing", {}), skip)
        runner = AnalysisRunner(folder, model, self)
        k = self._queue_total - len(self._queue)
        n = self._queue_total
        prefix = f"Song {k}/{n} · {song.name}: " if n > 1 else ""

        def prog(frac: float, msg: str) -> None:
            self.progress.emit(((k - 1) + frac) / n, prefix + msg)

        runner.progress.connect(prog)
        runner.finished.connect(lambda res, sid=sid, r=runner: self._job_finished(sid, res, r))
        runner.failed.connect(lambda err, sid=sid: self._job_failed(sid, err))
        self._runner = runner
        self._analysis_relay = None
        try:
            runner.start()
        except Exception as exc:
            self._job_failed(sid, f"Could not start the analysis: {exc}")

    def cancel_analysis(self) -> None:
        if self._analysis_relay is not None:
            self._analysis_relay.job.cancel_requested = True
        if getattr(self, "_queue_active", False):
            self._queue = []
            r = getattr(self, "_runner", None)
            self._runner = None
            self._queue_active = False
            if r is not None:
                r.cancel()                     # kills the worker and removes its folder now
                r.deleteLater()
            self.busy.emit("")
            self.status.emit("Analysis cancelled")

    def _job_finished(self, sid: str, res: AnalysisResult, runner) -> None:
        if getattr(self, "drag_active", False):
            # don't snapshot undo in the middle of a drag on the timeline: try again shortly
            from PySide6.QtCore import QTimer
            QTimer.singleShot(250, lambda: self._job_finished(sid, res, runner))
            return
        runner.deleteLater()
        self.settings.set("analysis_timing", runner.model.learn())
        song = self.project.song_by_id(sid)
        if song is None:
            self._job_failed(sid, "The song was removed while it was being analysed", runner=None)
            return
        try:
            self._apply_to_song(sid, res)
        except Exception:
            import traceback
            self._job_failed(sid, "Could not apply the results:\n" + traceback.format_exc(), runner=None)
            return
        self._queue_ok += 1
        if self._queue_total > 1:
            added = next((l for l in reversed(res.log) if "new suggestion" in l), res.log[-1] if res.log else "done")
            self._queue_log.append(f"{song.name}: {added}")
        self._queue_last = res
        self._start_next()

    def _job_failed(self, sid: str, err: str, runner="current") -> None:
        if runner == "current" and getattr(self, "_runner", None) is not None:
            self._runner.deleteLater()
        song = self.project.song_by_id(sid)
        err = (err or "").strip() or "The analysis stopped without a message"
        if err == "Cancelled":
            self._queue = []
            self._queue_active = False
            self._runner = None
            self.busy.emit("")
            self.status.emit("Analysis cancelled")
            return
        self._queue_errors.append(err)
        if self._queue_total > 1:
            self._queue_log.append(f"{song.name if song else '?'}: FAILED — {err.splitlines()[-1][:200]}")
        self._start_next()

    def _apply_to_song(self, origin: str, res: AnalysisResult) -> None:
        if not self.project.song_by_id(origin):
            self.status.emit("Analysis finished, but its song was removed")
            return
        current = self.project.song.id
        self.project.select_song(origin)   # apply results to the song that was analysed
        try:
            self._apply_analysis(res)
            from datetime import datetime
            self.project.song.analysed = datetime.now().isoformat(timespec="seconds")
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

    def run_analysis_in_thread(self, opts: AnalysisOptions) -> bool:
        """The old in-process path (kept for scripting and as a fallback)."""
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

    def _analysis_finished(self, res: AnalysisResult) -> None:
        self._analysis_relay = None
        self.busy.emit("")
        self._apply_to_song(getattr(self, "_analysis_song", self.project.song.id), res)
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
