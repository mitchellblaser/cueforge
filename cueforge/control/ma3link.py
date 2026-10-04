"""Live link to grandMA3 over OSC: preview, cue-list sync and timecode push.

grandMA3 executes command-line text it receives on its OSC "cmd" address when *Receive
Command* is enabled for the OSC line (In & Out ▸ OSC). Default address: /cmd (MA's documented command address). No
plugin has to run on the console for this.

* Live preview — while CueForge plays, cues fire on the console as the playhead passes
  them (resyncing every lane to its current cue whenever playback starts or jumps).
* Cue-list sync — cues created / renamed in CueForge are created / labelled in the
  target sequences (debounced). Deleting cues on the console is opt-in.
* Timecode push — each song's timecode XML is sent over OSC inside Lua commands, written
  to the console's own timecode library by Lua on the console and imported into the
  song's Timecode slot (onPC or a networked console; no paths to set).
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from ..core.editing import effective_cue_numbers, temp_cue_label
from ..core.model import lane_per_song


@dataclass
class LinkSettings:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    address: str = "/cmd"
    addr_v2: bool = True          # settings already moved off the old /gma3/cmd default
    preview: bool = True          # fire cues on the console while CueForge plays
    sync_cues: bool = True        # create / label cues as you program
    allow_delete: bool = False    # delete console cues that were deleted in CueForge
    all_songs: bool = False       # sync every song's cues (else only the open song)
    push_timecode: bool = False   # send + import timecode automatically after changes
    timecode_dir: str = ""        # unused (kept so old settings load); the push needs no path
    max_cmd: int = 200            # longest command the push sends (the console cuts long ones)
    pin_numbers: bool = True      # fix a cue's number once it exists on the console
    # command templates ({seq}, {cue}, {label}) — edit if your MA3 version wants other syntax
    cmd_goto: str = "Goto Sequence {seq} Cue {cue}"
    cmd_go: str = "Go+ Sequence {seq}"
    cmd_temp: str = "Temp Sequence {seq} Cue {cue}"
    cmd_temp_off: str = "Off Sequence {seq}"
    cmd_store: str = "Store Sequence {seq} Cue {cue} /Merge /NoConfirm"
    cmd_label: str = 'Label Sequence {seq} Cue {cue} "{label}"'
    cmd_delete: str = "Delete Sequence {seq} Cue {cue} /NoConfirm"
    cmd_label_seq: str = 'Label Sequence {seq} "{label}"'
    cmd_fade: str = "Sequence {seq} Cue {cue} CueFade {fade}"


# Network push: the console has no access to this computer's disk, so the timecode travels
# inside `Lua "…"` commands and Lua on the console writes it to the console's own disk.
# MA's command line cuts long commands off, so every command stays short: the payload goes
# as hex pieces into a Lua table (CF_P) and one last short command joins, decodes and runs
# it. The script it runs finds the console's timecode folder (cf_dir), fills in the
# sequence / cue addresses, writes the XML and imports it.
CONSOLE_DIR_LUA = """
local function cf_dir()
  local tries = {
    function() return GetPath(Enums.PathType.Library) .. '/datapools/timecodes' end,
    function() return GetPath('library', true) .. '/datapools/timecodes' end,
    function() return GetPath(Enums.PathType.UserTimecodes) end,
  }
  for _, fn in ipairs(tries) do
    local ok, d = pcall(fn)
    if ok and type(d) == 'string' and d ~= '' then
      local t = io.open(d .. '/CueForge_probe.tmp', 'w')
      if t then t:close() os.remove(d .. '/CueForge_probe.tmp') return d end
    end
  end
end
"""

MAX_CMD = 200              # characters per command (MA truncates long command lines)


def import_script(xml: str, slot: int, fix_src: str) -> str:
    """Lua the console runs once the push has arrived: expand the shortened XML, fill in the
    sequence / cue addresses from this show, write it into the console's timecode library,
    then delete and import the timecode slot (in that order, inside one script)."""
    from ..export.ma3 import RC_FLAGS, RC_HEAD
    # what every event repeats becomes one byte; the console expands it again
    frags = [RC_HEAD, RC_FLAGS, '<CmdEvent Name="', '" Time="', '" CueDestination="', 'Object="@SEQ',
             '" ExecToken="', '" ValCueDestination="@CUE', '@"/>\n</CmdEvent>\n', 'Status="On" ', 'Status="Off" ']
    codes = [c for c in range(1, 32) if c not in (9, 10, 13)][:len(frags)]
    packed = "\n".join(line.lstrip("\t") for line in xml.split("\n"))       # indentation isn't needed
    for code, frag in zip(codes, frags):
        packed = packed.replace(frag, chr(code))
    level = "===="
    while any(f"]{level}]" in s for s in [packed] + frags):
        level += "="

    def long(s: str) -> str:
        return "[" + level + "[" + ("\n" + s if s.startswith("\n") else s) + "]" + level + "]"
    table = ", ".join(f"[{c}] = {long(f)}" for c, f in zip(codes, frags))
    return ("local cf_xml = " + long(packed) + "\n"
            "local cf_rc = {" + table + "}\n"
            "cf_xml = cf_xml:gsub('[\\1-\\31]', function(c) return cf_rc[string.byte(c)] end)\n"
            + fix_src + "\n" + CONSOLE_DIR_LUA + "\n"
            "local d = cf_dir()\n"
            "if not d then ErrPrintf('CueForge: no writable timecode folder on this console') return end\n"
            "local _, n = cf_xml:gsub('@[SC][EU][QE]%d', '')\n"
            f"local fname = 'CueForge_{slot}.xml'\n"
            "local f = io.open(d .. '/' .. fname, 'w') f:write(cf_fix(cf_xml)) f:close()\n"
            f"Cmd('Delete Timecode {slot} /NoConfirm')\n"
            f"Cmd('Import Timecode {slot} /File \"' .. fname .. '\" /NoConfirm')\n"
            f"Printf('CueForge: Timecode {slot} imported, %d sequence/cue addresses filled in', n)\n")


# The last command: join the pieces, check they all arrived, decode the hex and run it.
DECODE_LUA = "CF_D=function(h) return (h:gsub('..',function(x) return string.char(tonumber(x,16)) end)) end"
# (a missing piece in the middle makes table.concat fail; the last one is checked by hand)
RUN_LUA = ("local p=CF_P CF_P=nil local ok,h=pcall(table.concat,p) if not (ok and p[{n}]) then "
           "return ErrPrintf('CueForge: lost data, push again') end assert(load(CF_D(h)))()")


def push_commands(script: str, max_len: int = MAX_CMD) -> list[str]:
    """Short `Lua "…"` commands that carry `script` to the console and run it there: the
    script as hex pieces into the Lua table CF_P (hex keeps every character the console's
    command line might interpret out of the way), the hex decoder, then RUN_LUA. Pieces are
    at most max_len characters; the two fixed commands are about 100 and 190."""
    data = script.encode("utf-8").hex().upper()
    head = "Lua \"CF_P=CF_P or {{}} CF_P[{i}]='"
    room = max(20, max_len - len(head.format(i=99999)) - 2)
    room -= room % 2                               # whole bytes per piece
    pieces = [data[i:i + room] for i in range(0, len(data), room)]
    cmds = ['Lua "CF_P={}"']
    cmds += [head.format(i=k) + piece + "'\"" for k, piece in enumerate(pieces, 1)]
    cmds.append('Lua "' + DECODE_LUA + '"')
    cmds.append('Lua "' + RUN_LUA.format(n=len(pieces)) + '"')
    return cmds


def _q(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def _num(n: float) -> str:
    return f"{n:g}"


class MA3Link(QObject):
    status = Signal(str)
    sent = Signal(str)            # every command (for the log)

    def __init__(self, session) -> None:
        super().__init__()
        self.s = session
        self.cfg = self._load()
        self._client = None
        self.log: list[str] = []
        self._last_pos: float | None = None
        self._was_playing = False
        self._last_wall = 0.0
        self._preview_cache = None
        self._offs: list[tuple[float, int]] = []            # pending Temp releases (time, sequence)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(600)
        self._sync_timer.timeout.connect(self.sync_cues)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(15)
        self._tick_timer.timeout.connect(self.tick)
        self._tc_timer = QTimer(self)
        self._tc_timer.setSingleShot(True)
        self._tc_timer.setInterval(3000)
        self._tc_timer.timeout.connect(self._maybe_push_timecode)
        for sig in (session.cues_changed, session.lanes_changed, session.songs_changed):
            sig.connect(self._changed)
        session.project_replaced.connect(self._project_replaced)

    # ------------------------------------------------------------------ setup
    def cmd(self, name: str, seq: int, cue: float | None = None, label: str = "", fade: float = 0.0) -> str:
        args = dict(seq=seq, cue="" if cue is None else _num(cue), label=_q(label), fade=_num(round(fade, 3)))
        try:
            return (getattr(self.cfg, "cmd_" + name) or "").format(**args) or \
                getattr(LinkSettings, "cmd_" + name).format(**args)
        except (KeyError, IndexError, ValueError):
            return getattr(LinkSettings, "cmd_" + name).format(**args)

    def _load(self) -> LinkSettings:
        d = dict(self.s.settings.get("ma3link", {}) or {})
        if d and not d.get("addr_v2"):              # once: the old default /gma3/cmd -> /cmd
            if d.get("address") == "/gma3/cmd":
                d["address"] = "/cmd"
        d["addr_v2"] = True
        return LinkSettings(**{k: v for k, v in d.items() if k in LinkSettings.__dataclass_fields__})

    def save(self) -> None:
        self.s.settings.set("ma3link", asdict(self.cfg))

    def apply(self) -> None:
        self.release_temps()                   # with the old connection, before it goes
        self._client = None
        self._tick_timer.stop()
        self._was_playing = False
        self._preview_cache = None
        if not self.cfg.enabled:
            self.status.emit("MA3 link off")
            return
        try:
            from pythonosc.udp_client import SimpleUDPClient
            self._client = SimpleUDPClient(self.cfg.host, int(self.cfg.port))
        except Exception as exc:
            self.status.emit(f"MA3 link: {exc}")
            return
        self._tick_timer.start()
        self.status.emit(f"MA3 link → {self.cfg.host}:{self.cfg.port} {self.cfg.address}")
        if self.cfg.sync_cues:
            self._sync_timer.start()

    @property
    def active(self) -> bool:
        return self._client is not None

    def send(self, cmd: str) -> None:
        self.log.append(cmd)
        del self.log[:-300]
        self.sent.emit(cmd)
        if self._client is not None:
            try:
                self._client.send_message(self.cfg.address, cmd)
            except Exception as exc:
                self.status.emit(f"MA3 link send failed: {exc}")

    # ------------------------------------------------------------------ helpers
    def _lane_cues(self, project):
        """(lane, sequence, {cue id: number}, cues) for exported lanes of the current song."""
        from ..export.ma3 import export_lanes, seq_number
        for lane in export_lanes(project):
            yield lane, seq_number(project, lane), effective_cue_numbers(project, lane.id), \
                project.cues_in_lane(lane.id)

    def _preview_lanes(self):
        """Cached (sequence, {cue id: number}, cues, {cue id: token}) of the open song."""
        from ..export.ma3 import cue_tokens
        p = self.s.project
        key = (id(p), p.song.id)
        if self._preview_cache is None or self._preview_cache[0] != key:
            self._preview_cache = (key, [(seq, nums, list(cues), cue_tokens(p, lane.id))
                                         for lane, seq, nums, cues in self._lane_cues(p)])
        return self._preview_cache[1]

    # ------------------------------------------------------------------ live preview
    def tick(self) -> None:
        if not self.active or not self.cfg.preview:
            if self._offs:
                self.release_temps()
            self._was_playing = False
            return
        eng = self.s.engine
        playing = eng.playing
        pos = eng.position()
        if not playing:
            if self._was_playing:
                self._was_playing = False
                self.release_temps()           # don't leave a strobe running when you stop
            self._last_pos = pos
            return
        now = time.monotonic()
        # a forward step bigger than the wall-clock time allows is a seek (UI stalls are not)
        allowed = 0.5 + 2.0 * max(1.0, getattr(eng, "speed", 1.0) or 1.0) * (now - self._last_wall)
        if not self._was_playing or self._last_pos is None or pos < self._last_pos - 0.05 \
                or pos - self._last_pos > allowed:
            self.resync(pos)                       # started or jumped: put every lane on its cue
        else:
            self.fire_between(self._last_pos, pos)
        self._was_playing = True
        self._last_pos = pos
        self._last_wall = now

    def release_temps(self) -> None:
        for seq in sorted({seq for _, seq in self._offs}):
            self.send(self.cmd("temp_off", seq))
        self._offs = []

    def resync(self, t: float) -> None:
        self.release_temps()
        for seq, nums, cues, _ in self._preview_lanes():
            before = [c for c in cues if c.time <= t + 1e-6]
            plain = [c for c in before if not c.duration]
            held = [c for c in before if c.duration and c.time + c.duration > t]
            if plain:
                self.send(self.cmd("goto", seq, nums[plain[-1].id]))
            if held:
                c = held[-1]
                self.send(self.cmd("temp", seq, nums[c.id]))
                self._offs.append((c.time + c.duration, seq))

    def fire_between(self, t0: float, t1: float) -> None:
        events = []
        for seq, nums, cues, toks in self._preview_lanes():
            for c in cues:
                if t0 < c.time <= t1:
                    events.append((c.time, seq, nums[c.id], c, toks.get(c.id, "Goto")))
        for t, seq, num, c, tok in sorted(events, key=lambda e: e[0]):
            # a new Temp in a lane replaces that lane's pending release
            self._release_due(t)
            if c.duration:
                self._offs = [o for o in self._offs if o[1] != seq]
                self.send(self.cmd("temp", seq, num))
                self._offs.append((t + c.duration, seq))
            elif tok == "Go+":
                self.send(self.cmd("go", seq))
            else:
                self.send(self.cmd("goto", seq, num))
        self._release_due(t1)

    def _release_due(self, t: float) -> None:
        due = sorted(o for o in self._offs if o[0] <= t)
        self._offs = [o for o in self._offs if o[0] > t]
        for _, seq in due:
            self.send(self.cmd("temp_off", seq))

    # ------------------------------------------------------------------ cue-list sync
    def _changed(self, *_) -> None:
        self._preview_cache = None
        if self.active and self.cfg.sync_cues:
            self._sync_timer.start()
        if self.active and self.cfg.push_timecode:
            self._tc_timer.start()

    def _project_replaced(self) -> None:
        # a song switch or undo also lands here; the console record lives in the project
        self._preview_cache = None
        self._last_pos = None
        self._changed()

    @property
    def record(self) -> dict:
        """{cue id: [seq, number, label]} of cues already created on the console."""
        return self.s.project.console.setdefault("cues", {})

    @property
    def record_seqs(self) -> dict:
        return self.s.project.console.setdefault("seqs", {})

    # kept for callers/tests that think in (seq, cue) terms
    @property
    def pushed(self) -> dict[tuple[int, float], str]:
        return {(int(v[0]), float(v[1])): v[2] for v in self.record.values()}

    def _scope(self):
        p = self.s.project
        return p.songs if self.cfg.all_songs else [p.song]

    def desired(self) -> dict[str, tuple[int, float, str]]:
        """{cue id: (sequence, number, label)} for the songs in scope. Cues already on the
        console keep the number they were created with even if undo cleared it."""
        from ..export.ma3 import in_song
        p = self.s.project
        rec = self.record
        unpin = set(self.s.project.console.get("unpin", []))   # numbered automatically on purpose
        want: dict[str, tuple[int, float, str]] = {}
        self.want_fade: dict[str, float | None] = {}   # cue id -> fade (None: no fade set)
        for song in self._scope():
            with in_song(p, song):
                for lane, seq, nums, cues in self._lane_cues(p):
                    for c in cues:
                        if c.number is None and c.id in rec and self.cfg.pin_numbers \
                                and int(rec[c.id][0]) == seq and c.id not in unpin:
                            c.number = float(rec[c.id][1])       # undo removed a pinned number
                            nums = effective_cue_numbers(p, lane.id)
                    tlabel = temp_cue_label(p, lane.id) if any(c.duration for c in cues) else ""
                    tfade = next((c.fade for c in cues if c.duration and c.fade), None)
                    for c in cues:                         # all Temps share the lane's Temp cue
                        want[c.id] = (seq, float(nums[c.id]), tlabel if c.duration else (c.label or ""))
                        self.want_fade[c.id] = tfade if c.duration else (c.fade or None)
        return want

    def desired_cues(self) -> dict[tuple[int, float], str]:
        return {(seq, num): label for seq, num, label in self.desired().values()}

    def desired_sequences(self) -> dict[int, str]:
        from ..export.ma3 import in_song
        p = self.s.project
        out: dict[int, str] = {}
        for song in self._scope():
            with in_song(p, song):
                for lane, seq, _, _ in self._lane_cues(p):
                    # a per-song sequence is named after its song; a shared one after its lane
                    own = lane_per_song(lane) and len(p.songs) > 1
                    out[seq] = f"{song.name} {lane.name}" if own else lane.name
        return out

    def sync_cues(self) -> list[str]:
        """Send the commands that bring the console's cue lists in line with CueForge.

        Only cues created by the link are ever deleted, and only when their CueForge cue
        is gone (not when it merely left the sync scope or its lane stopped exporting)."""
        if not self.active:
            return []
        p = self.s.project
        rec = self.record
        want = self.desired()
        taken = {(seq, num) for seq, num, _ in want.values()}
        on_console = {(int(v[0]), float(v[1])) for v in rec.values()}
        cmds: list[str] = []
        labelled = {(int(v[0]), float(v[1])): v[2] for v in rec.values()}   # what each console cue is called
        faded = {(int(v[0]), float(v[1])): (v[3] if len(v) > 3 else None) for v in rec.values()}
        for cid, (seq, num, label) in sorted(want.items(), key=lambda kv: (kv[1][0], kv[1][1])):
            old = rec.get(cid)
            same = old is not None and (int(old[0]), float(old[1])) == (seq, num)
            if same:
                if label and label != old[2] and labelled.get((seq, num)) != label:
                    cmds.append(self.cmd("label", seq, num, label))
                    labelled[(seq, num)] = label
            else:
                if (seq, num) not in on_console:            # Temps sharing a cue store it once
                    cmds.append(self.cmd("store", seq, num))
                    on_console.add((seq, num))
                if label and labelled.get((seq, num)) != label:
                    cmds.append(self.cmd("label", seq, num, label))
                    labelled[(seq, num)] = label
                if old is not None and self.cfg.allow_delete and (int(old[0]), float(old[1])) not in taken:
                    cmds.append(self.cmd("delete", int(old[0]), float(old[1])))   # renumbered
            fade = self.want_fade.get(cid)
            sent = faded.get((seq, num))
            if fade is not None and sent != fade:          # new or changed fade
                cmds.append(self.cmd("fade", seq, num, fade=fade))
            elif fade is None and sent:                     # fade removed: back to 0
                cmds.append(self.cmd("fade", seq, num, fade=0.0))
            faded[(seq, num)] = fade
            rec[cid] = [seq, num, label or (old[2] if old and (int(old[0]), float(old[1])) == (seq, num) else ""), fade]
            if cid in p.console.get("unpin", []):
                p.console["unpin"].remove(cid)            # the console now has the new number
        existing = {c.id for song in p.songs for c in song.cues}
        for cid in [c for c in rec if c not in existing]:
            seq, num = int(rec[cid][0]), float(rec[cid][1])
            if not self.cfg.allow_delete:
                break
            if (seq, num) not in taken:
                cmds.append(self.cmd("delete", seq, num))
            del rec[cid]
        # name the sequences after the lanes (after their first cue has created them)
        has_cues = {int(v[0]) for v in rec.values()}
        seqs = self.record_seqs
        for seq, name in sorted(self.desired_sequences().items()):
            if seq in has_cues and seqs.get(str(seq)) != name:
                cmds.append(self.cmd("label_seq", seq, label=name))
                seqs[str(seq)] = name
        for c in cmds:
            self.send(c)
        pinned = self.pin_numbers() if self.cfg.pin_numbers else 0
        if cmds or pinned:
            self._touch()
        if pinned:
            self.s.cues_changed.emit()
        if cmds:
            self.status.emit(f"MA3 link: {len(cmds)} command(s) sent")
        return cmds

    def _touch(self) -> None:
        touch = getattr(self.s, "_touch", None)
        if touch:
            touch()

    def push_all(self) -> list[str]:
        """Forget what the console has and create every cue again (e.g. a fresh show)."""
        self.s.project.console = {}
        return self.sync_cues()

    def pin_numbers(self) -> int:
        """Give every synced cue its current number explicitly, so inserting a cue later
        gets a point number (5.1) instead of shifting cues that already hold looks."""
        from ..export.ma3 import in_song
        p = self.s.project
        rec = self.record
        n = 0
        for song in self._scope():
            with in_song(p, song):
                for lane, seq, nums, cues in self._lane_cues(p):
                    for c in cues:
                        if c.number is None and c.id in rec:
                            c.number = nums[c.id]
                            n += 1
        return n

    # ------------------------------------------------------------------ timecode push
    def _maybe_push_timecode(self) -> None:
        if not (self.active and self.cfg.push_timecode):
            return
        if self._sync_timer.isActive():        # let the cues exist first
            self._tc_timer.start()
            return
        self.push_timecode()

    def push_timecode(self) -> list[str]:
        """Send the timecode of the open song (or every song) to the console and import it
        there. Everything travels over OSC and is written to the console's own disk, so a
        networked console works the same as onPC on this computer — no paths to set."""
        from ..export.ma3 import SEQ_FIX_LUA, build_ma3_xml, export_lanes, in_song
        p = self.s.project
        cmds = []
        songs = 0
        for song in (p.songs if self.cfg.all_songs else [p.song]):
            with in_song(p, song):
                if not export_lanes(p):
                    continue
                script = import_script(build_ma3_xml(p, placeholders=True), song.ma3_timecode, SEQ_FIX_LUA)
                cmds += push_commands(script, int(self.cfg.max_cmd or MAX_CMD))
                songs += 1
        for c in cmds:
            self.send(c)
            time.sleep(0.003)                  # pace the packets for the console's OSC input
        self.status.emit(f"Timecode pushed ({songs} song(s), {len(cmds)} commands)")
        return cmds
