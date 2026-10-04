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
    auto_pull: bool = True        # bring timecode edits made on the console back (while linked)
    reply_port: int = 8001        # CueForge listens here for the console's replies
    reply_line: int = 2           # the console's OSC line that sends to CueForge (SendOSC <line>)
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


def import_script(xml: str, slot: int, fix_src: str, names: list[str] | None = None,
                  offset: str = "0h00m00.000") -> str:
    """Lua the console runs once the push has arrived: expand the shortened XML, fill in the
    sequence / cue addresses (sequences found by `names`, in seq_table order), write it into
    the console's timecode library, delete and import the timecode slot, then set its
    Offset TC Slot to the song's start (`offset`) — all in that order, inside one script."""
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
    seqs = ", ".join(long(n) for n in (names or []))
    return ("local cf_names = {" + seqs + "}\n"
            "local cf_xml = " + long(packed) + "\n"
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
            f"Printf('CueForge: Timecode {slot} imported, %d sequence/cue addresses filled in', n)\n"
            f"cf_set_offset({slot}, '{offset}')\n")


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


class OscSender:
    """Sends OSC on a background thread. Two queues: live commands go first; bulk commands
    keep their order and are paced (BULK_GAP) so a long push doesn't flood the console."""
    BULK_GAP = 0.003

    def __init__(self, on_error=None) -> None:
        import queue
        import threading
        self._live: queue.Queue = queue.Queue()
        self._bulk: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._on_error = on_error
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="ma3-osc", daemon=True).start()

    def put(self, client, address: str, cmd: str, bulk: bool = False) -> None:
        with self._lock:
            (self._bulk if bulk else self._live).put((client, address, cmd))
            self._idle.clear()
        self._wake.set()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until everything queued so far has gone out (tests, shutdown)."""
        return self._idle.wait(timeout)

    def _send(self, item) -> None:
        client, address, cmd = item
        try:
            client.send_message(address, cmd)
        except Exception as exc:
            if self._on_error:
                self._on_error(f"MA3 link send failed: {exc}")

    def _run(self) -> None:
        import queue
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                try:
                    self._send(self._live.get_nowait())
                    continue
                except queue.Empty:
                    pass
                try:
                    item = self._bulk.get_nowait()
                except queue.Empty:
                    break
                self._send(item)
                time.sleep(self.BULK_GAP)
            with self._lock:
                if self._live.empty() and self._bulk.empty():
                    self._idle.set()


def _q(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def _num(n: float) -> str:
    return f"{n:g}"


class MA3Link(QObject):
    status = Signal(str)
    sent = Signal(str)            # every command (for the log)
    pulled = Signal(int, str)     # the console's timecode for a slot (from the reply thread)
    console_changed = Signal(str)  # a summary after console edits were brought in

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
        self._sender = OscSender(lambda msg: self.status.emit(msg))
        self._pushed_tc: dict[str, str] = {}               # song id -> timecode script last pushed
        self._pushed_items: dict[str, list] = {}            # song id -> its events as last pushed
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
        # the other way: timecode edits made on the console come back (ma3pull)
        self._receiver = None
        self._pull_base: dict[str, tuple] = {}            # song id -> console version last seen
        self._pull_defined = False                        # CF_PULL sent to the console
        self._pull_unanswered = 0
        self._pull_manual = False
        self.pulled.connect(self._on_pulled)
        self._pull_timer = QTimer(self)
        self._pull_timer.setInterval(10000)
        self._pull_timer.timeout.connect(self._poll_pull)

    # ------------------------------------------------------------------ setup
    def cmd(self, name: str, seq, cue: float | None = None, label: str = "", fade: float = 0.0) -> str:
        """A command from its template. `seq` is the sequence's name (sent quoted, so the
        console finds it wherever it sits) or, for old-style calls, its number."""
        seq_ref = f'"{_q(seq)}"' if isinstance(seq, str) else seq
        args = dict(seq=seq_ref, cue="" if cue is None else _num(cue), label=_q(label), fade=_num(round(fade, 3)))
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
        self._pushed_tc = {}                   # a new connection may be a different console
        self._pull_timer.stop()
        self._pull_base = {}
        self._pull_defined = False
        self._pull_unanswered = 0
        if self._receiver is not None:
            self._receiver.close()
            self._receiver = None
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
        self._start_receiver()
        if self.cfg.auto_pull:
            self._pull_timer.start()

    # ------------------------------------------------------------------ console -> CueForge
    def _start_receiver(self) -> bool:
        if self._receiver is not None:
            return True
        try:
            from .ma3pull import PullReceiver
            self._receiver = PullReceiver(int(self.cfg.reply_port), lambda slot, text: self.pulled.emit(slot, text))
            return True
        except Exception as exc:
            self.status.emit(f"MA3 link: can't listen for the console on port {self.cfg.reply_port}: {exc}")
            return False

    def _pull_cmds(self, force: bool, slots: list[int]) -> list[str]:
        from .ma3pull import PULL_LUA
        cmds = []
        if not self._pull_defined:
            cmds += push_commands(CONSOLE_DIR_LUA + PULL_LUA, int(self.cfg.max_cmd or MAX_CMD))
            self._pull_defined = True
        args = ",".join(str(int(s)) for s in slots)
        cmds.append(f'Lua "CF_PULL({int(self.cfg.reply_line)},{"true" if force else "false"},{args})"')
        return cmds

    def pull_now(self) -> list[str]:
        """Ask the console for the open song's timecode and take its version (the button)."""
        if not self.active or not self._start_receiver():
            return []
        self._pull_manual = True
        self._pull_unanswered = 0
        cmds = self._pull_cmds(True, [self.s.project.song.ma3_timecode])
        for c in cmds:
            self.send(c, bulk=True)
        self.status.emit("Asked the console for its timecode…")
        return cmds

    def _poll_pull(self) -> None:
        """Every few seconds while linked and stopped: has the open song's timecode been
        edited on the console? (The console answers 'same' when nothing changed.)"""
        if not (self.active and self.cfg.auto_pull) or getattr(self.s.engine, "playing", False):
            return
        if self._sync_timer.isActive() or self._tc_timer.isActive() or not self._sender.wait_idle(0):
            return                              # CueForge's own changes are still going out
        if self._pull_unanswered >= 2:
            self._pull_defined = False          # console restarted? send CF_PULL again
        if self._pull_unanswered >= 5:
            self._pull_timer.stop()
            self.status.emit(f"MA3 link: no reply from the console — set up an OSC line that sends to this "
                             f"computer, port {self.cfg.reply_port} (see the live link settings)")
            return
        self._pull_unanswered += 1
        for c in self._pull_cmds(False, [self.s.project.song.ma3_timecode]):
            self.send(c, bulk=True)

    def _on_pulled(self, slot: int, text: str) -> None:
        """The console's version of a timecode show arrived (UI thread)."""
        from .ma3pull import SPECIAL, merge_into_song, own_items, parse_pull, signature
        from ..export.ma3_import import show_to_cues
        self._pull_unanswered = 0
        manual, self._pull_manual = self._pull_manual, False
        p = self.s.project
        song = p.song
        if song.ma3_timecode != slot:
            return                              # the user moved to another song meanwhile
        if text in SPECIAL:
            if manual and text != "same":
                self.status.emit(f"MA3 link: Timecode {slot} " + ("doesn't exist on the console" if text == "none"
                                                                 else "could not be exported on the console"))
            return
        items = show_to_cues(parse_pull(text))
        theirs = signature(items)
        ours = signature(own_items(p))
        base = self._pull_base.get(song.id)
        if theirs == ours:
            self._pull_base[song.id] = theirs
            if manual:
                self.status.emit("MA3 link: the console's timecode matches CueForge")
            return
        if not manual:
            if base is None:
                self._pull_base[song.id] = theirs
                self.status.emit(f"MA3 link: '{song.name}' differs on the console — Pull from console takes the "
                                 "console's version, Push timecode sends CueForge's")
                return
            if theirs == base:
                return                          # the console didn't change: CueForge's edits will be pushed
            if ours != signature(self._last_pushed_items(song)):
                self._pull_base[song.id] = theirs
                self.status.emit(f"MA3 link: '{song.name}' was edited on the console and in CueForge — "
                                 "kept CueForge's; Pull from console takes the console's version")
                return
        with self.s.edit("Timecode edits from the console"):
            stats = merge_into_song(p, items)
        self._pull_base[song.id] = theirs
        self._remember_pushed(song)             # in line with the console now: nothing to push back
        self.s.cues_changed.emit()
        msg = (f"From the console: {stats['moved']} moved, {stats['added']} added, {stats['removed']} removed"
               + (f" (no lane for {', '.join(stats['unmatched'])})" if stats["unmatched"] else ""))
        self.status.emit(msg)
        self.console_changed.emit(msg)

    def _last_pushed_items(self, song):
        """What CueForge last pushed for a song, as items (its current items if never pushed)."""
        from .ma3pull import own_items
        return self._pushed_items.get(song.id) or own_items(self.s.project)

    def _remember_pushed(self, song) -> None:
        from ..export.ma3 import SEQ_FIX_LUA, build_ma3_xml, offset_text, seq_table
        from .ma3pull import own_items
        p = self.s.project
        self._pushed_items[song.id] = own_items(p)
        self._pushed_tc[song.id] = import_script(build_ma3_xml(p, placeholders=True), song.ma3_timecode,
                                                 SEQ_FIX_LUA, [n for n, _ in seq_table(p)], offset_text(p.tc_offset))

    @property
    def active(self) -> bool:
        return self._client is not None

    def send(self, cmd: str, bulk: bool = False) -> None:
        """Queue a command for the sender thread, so the UI never waits on the network.
        Live commands (preview Go / Temp) go out at once, ahead of bulk work (cue sync, a
        timecode push), which is paced so the console's OSC input keeps up."""
        self.log.append(cmd)
        del self.log[:-300]
        self.sent.emit(cmd)
        if self._client is not None:
            self._sender.put(self._client, self.cfg.address, cmd, bulk)

    # ------------------------------------------------------------------ helpers
    def _lane_cues(self, project):
        """(lane, sequence name, {cue id: number}, cues) for exported lanes of the current song."""
        from ..export.ma3 import export_lanes, seq_name
        for lane in export_lanes(project):
            yield lane, seq_name(project, lane), effective_cue_numbers(project, lane.id), \
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
        """{cue id: [sequence name, number, label, fade]} of cues already on the console.
        (Records from before sequences were found by name hold a sequence number.)"""
        return self.s.project.console.setdefault("cues", {})

    @property
    def made(self) -> list:
        """Names of sequences that exist on the console (created or adopted by the link)."""
        return self.s.project.console.setdefault("made", [])

    @property
    def seq_keys(self) -> dict:
        """{"song id:lane id": sequence name} last used, to rename instead of re-create."""
        return self.s.project.console.setdefault("seqkeys", {})

    # kept for callers/tests that think in (seq, cue) terms
    @property
    def pushed(self) -> dict[tuple, str]:
        return {(v[0], float(v[1])): v[2] for v in self.record.values()}

    def _scope(self):
        p = self.s.project
        return p.songs if self.cfg.all_songs else [p.song]

    def desired(self) -> dict[str, tuple[str, float, str]]:
        """{cue id: (sequence name, number, label)} for the songs in scope. Cues already on
        the console keep the number they were created with even if undo cleared it. Also
        fills want_fade, want_seq ({name: (key, creation number, first cue)})."""
        from ..export.ma3 import in_song, seq_number
        p = self.s.project
        rec = self.record
        unpin = set(self.s.project.console.get("unpin", []))   # numbered automatically on purpose
        want: dict[str, tuple[str, float, str]] = {}
        self.want_fade: dict[str, float | None] = {}   # cue id -> fade (None: no fade set)
        self.want_seq: dict[str, tuple[str, int, float]] = {}
        for song in self._scope():
            with in_song(p, song):
                for lane, seq, nums, cues in self._lane_cues(p):
                    shared = not lane_per_song(lane)
                    for c in cues:
                        if c.number is None and c.id in rec and self.cfg.pin_numbers and rec[c.id][0] == seq \
                                and c.id not in unpin and not (shared and c.duration):
                            c.number = float(rec[c.id][1])       # undo removed a pinned number
                            nums = effective_cue_numbers(p, lane.id)
                    tlabel = temp_cue_label(p, lane.id) if any(c.duration for c in cues) else ""
                    tfade = next((c.fade for c in cues if c.duration and c.fade), None)
                    for c in cues:                         # all Temps share the lane's Temp cue
                        want[c.id] = (seq, float(nums[c.id]), tlabel if c.duration else (c.label or ""))
                        self.want_fade[c.id] = tfade if c.duration else (c.fade or None)
                    key = f"{song.id}:{lane.id}" if lane_per_song(lane) else f"*:{lane.id}"
                    first = min(nums[c.id] for c in cues) if cues else 1.0
                    if seq not in self.want_seq or first < self.want_seq[seq][2]:
                        self.want_seq[seq] = (key, seq_number(p, lane), first)
        return want

    def desired_cues(self) -> dict[tuple[str, float], str]:
        return {(seq, num): label for seq, num, label in self.desired().values()}

    def _sequences(self, want: dict, cmds: list[str]) -> None:
        """Make sure every wanted sequence exists under its name: rename one whose song or
        lane was renamed, adopt one created by number before (records holding a number), and
        create the rest on the console (CF_ENSURE: first free number from the lane's seq)."""
        from ..export.ma3 import ENSURE_LUA
        rec, made, keys = self.record, self.made, self.seq_keys
        # renamed song / lane: rename its sequence and the records pointing at it
        for name, (key, _, _) in self.want_seq.items():
            old = keys.get(key)
            if old and old != name and old in made and name not in made:
                cmds.append(f'Label Sequence "{_q(old)}" "{_q(name)}"')
                made[made.index(old)] = name
                for v in rec.values():
                    if v[0] == old:
                        v[0] = name
            keys[key] = name
        # records from the number-based scheme: the first song claiming a number adopts it
        adopted = self.s.project.console.setdefault("adopted", {})
        for cid, v in rec.items():
            if isinstance(v[0], str):
                continue
            num_key = str(int(v[0]))
            name = want[cid][0] if cid in want else None
            if num_key not in adopted and name and name not in made:
                cmds.append(f'Label Sequence {int(v[0])} "{_q(name)}"')
                adopted[num_key] = name
                made.append(name)
            v[0] = adopted.get(num_key, f"#{num_key}")      # not adopted: no longer ours by name
        missing = [n for n in self.want_seq if n not in made]
        if missing:
            cmds += push_commands(ENSURE_LUA, int(self.cfg.max_cmd or MAX_CMD))   # defines CF_ENSURE
            for name in missing:
                _, pref, first = self.want_seq[name]
                cmds.append(f'Lua "CF_ENSURE([[{name}]],{int(pref)},{_num(first)})"')
                made.append(name)

    def sync_cues(self) -> list[str]:
        """Send the commands that bring the console's cue lists in line with CueForge.

        Only cues created by the link are ever deleted, and only when their CueForge cue
        is gone (not when it merely left the sync scope or its lane stopped exporting)."""
        if not self.active:
            return []
        p = self.s.project
        rec = self.record
        want = self.desired()
        cmds: list[str] = []
        self._sequences(want, cmds)
        taken = {(seq, num) for seq, num, _ in want.values()}
        on_console = {(v[0], float(v[1])) for v in rec.values()}
        labelled = {(v[0], float(v[1])): v[2] for v in rec.values()}   # what each console cue is called
        faded = {(v[0], float(v[1])): (v[3] if len(v) > 3 else None) for v in rec.values()}
        for cid, (seq, num, label) in sorted(want.items(), key=lambda kv: (kv[1][0], kv[1][1])):
            old = rec.get(cid)
            same = old is not None and (old[0], float(old[1])) == (seq, num)
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
                if old is not None and self.cfg.allow_delete and (old[0], float(old[1])) not in taken \
                        and not str(old[0]).startswith("#"):
                    cmds.append(self.cmd("delete", old[0], float(old[1])))   # renumbered
            fade = self.want_fade.get(cid)
            sent = faded.get((seq, num))
            if fade is not None and sent != fade:          # new or changed fade
                cmds.append(self.cmd("fade", seq, num, fade=fade))
            elif fade is None and sent:                     # fade removed: back to 0
                cmds.append(self.cmd("fade", seq, num, fade=0.0))
            faded[(seq, num)] = fade
            rec[cid] = [seq, num, label or (old[2] if same else ""), fade]
            if cid in p.console.get("unpin", []):
                p.console["unpin"].remove(cid)            # the console now has the new number
        existing = {c.id for song in p.songs for c in song.cues}
        for cid in [c for c in rec if c not in existing]:
            seq, num = rec[cid][0], float(rec[cid][1])
            if not self.cfg.allow_delete:
                break
            if (seq, num) not in taken and not str(seq).startswith("#"):
                cmds.append(self.cmd("delete", seq, num))
            del rec[cid]
        for c in cmds:
            self.send(c, bulk=True)
        pinned = self.pin_numbers() if self.cfg.pin_numbers else set()
        if cmds or pinned:
            self._touch()
        if pinned:                              # numbers only shown changed style: repaint those lanes
            self.s.touched_lanes = pinned
            try:
                self.s.cues_changed.emit()
            finally:
                self.s.touched_lanes = None
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

    def pin_numbers(self) -> set[str]:
        """Give every synced cue its current number explicitly, so inserting a cue later
        gets a point number (5.1) instead of shifting cues that already hold looks.
        Returns the lanes that changed."""
        from ..export.ma3 import in_song
        p = self.s.project
        rec = self.record
        lanes: set[str] = set()
        for song in self._scope():
            with in_song(p, song):
                for lane, seq, nums, cues in self._lane_cues(p):
                    shared = not lane_per_song(lane)
                    for c in cues:
                        if shared and c.duration:
                            continue           # a shared lane's Temp cue is one cue for the setlist
                        if c.number is None and c.id in rec:
                            c.number = nums[c.id]
                            lanes.add(lane.id)
        return lanes

    # ------------------------------------------------------------------ timecode push
    def _maybe_push_timecode(self) -> None:
        if not (self.active and self.cfg.push_timecode):
            return
        if self._sync_timer.isActive() or getattr(self.s.engine, "playing", False):
            self._tc_timer.start()             # let the cues exist first; never re-import mid-song
            return
        self.push_timecode(only_changed=True)

    def push_timecode(self, only_changed: bool = False) -> list[str]:
        """Send the timecode of the open song (or every song) to the console and import it
        there. Everything travels over OSC and is written to the console's own disk, so a
        networked console works the same as onPC on this computer — no paths to set.
        `only_changed` (the automatic push) skips songs whose timecode is unchanged."""
        from ..export.ma3 import SEQ_FIX_LUA, build_ma3_xml, export_lanes, in_song, offset_text, seq_table
        p = self.s.project
        cmds = []
        songs = 0
        for song in (p.songs if self.cfg.all_songs else [p.song]):
            with in_song(p, song):
                if not export_lanes(p):
                    continue
                script = import_script(build_ma3_xml(p, placeholders=True), song.ma3_timecode, SEQ_FIX_LUA,
                                       [n for n, _ in seq_table(p)], offset_text(p.tc_offset))
                if only_changed and self._pushed_tc.get(song.id) == script:
                    continue
                self._pushed_tc[song.id] = script
                from .ma3pull import own_items
                self._pushed_items[song.id] = own_items(p)
                cmds += push_commands(script, int(self.cfg.max_cmd or MAX_CMD))
                songs += 1
        for c in cmds:
            self.send(c, bulk=True)            # paced by the sender thread, not here
        if songs:
            self.status.emit(f"Timecode pushed ({songs} song(s), {len(cmds)} commands)")
        return cmds
