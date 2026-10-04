"""Bring timecode edits made on grandMA3 back into CueForge (the other half of the live link).

The console has no way to hand CueForge a file, but it can send OSC (`SendOSC <line>`). A
Lua helper (CF_PULL, defined on the console once) exports a timecode show with the object
API (`handle:Export`), reads that XML back, boils it down to one line per track and event,
and sends it to CueForge in short `SendOSC` messages:

    /cueforge/tc  "<slot>:<part>:<parts>:<payload>"

The payload lines are `T|<track name>|<target>` and
`E|<time>|<token>|<status>|<cue handle>|<cue name>`, with `% " , | newline` escaped as %XX
so nothing upsets the console's command line or OSC's argument list. CF_PULL remembers a
fingerprint per slot and answers `same` when nothing changed, so polling stays cheap.

CueForge merges what arrives into its lanes (merge_into_song): cues keep their labels,
fades and notes; moved events move them, new events add cues, deleted events remove them.
"""
from __future__ import annotations

import html
import re
import threading

from ..core.model import Cue, Project
from ..export.ma3_import import TcEvent, TcShow, TcTrack, _cue_from_handle, _hms, show_to_cues

# Defines CF_PULL(line, force, slot, ...) on the console. Uses cf_dir() (CONSOLE_DIR_LUA),
# which the caller puts in front.
PULL_LUA = r"""
local function cf_pull_tc(no)
  local ok, kids = pcall(function() return DataPool().Timecodes:Children() end)
  for _, t in ipairs((ok and kids) or {}) do
    local ok2, n = pcall(function() return t.no end)
    if ok2 and tonumber(n) == no then return t end
  end
end
local function cf_esc(s)
  return (tostring(s or ''):gsub('[%%",|\n\r]', function(c) return string.format('%%%02X', string.byte(c)) end))
end
local function cf_attr(attrs, name) return attrs:match(' ' .. name .. '="([^"]*)"') end
function CF_PULL_SEND(line, no, i, n, payload)
  Cmd(string.format('SendOSC %d "/cueforge/tc,s,%d:%d:%d:%s"', line, no, i, n, payload))
end
function CF_PULL(line, force, ...)
  CF_LAST = CF_LAST or {}
  local d = cf_dir()
  for _, no in ipairs({...}) do
    local tc = cf_pull_tc(no)
    if not tc or not d then
      CF_PULL_SEND(line, no, 1, 1, 'none')
    else
      local fname = 'CueForge_pull_' .. no .. '.xml'
      pcall(function() return tc:Export(d, fname) end)
      local f = io.open(d .. '/' .. fname, 'r')
      if not f then
        CF_PULL_SEND(line, no, 1, 1, 'noexport')
      else
        local x = f:read('*a') f:close() os.remove(d .. '/' .. fname)
        local out, ev = {}, nil
        for tag, attrs in x:gmatch('<(%w+)([^>]*)>') do
          if tag == 'Track' then
            out[#out + 1] = 'T|' .. cf_esc(cf_attr(attrs, 'Name')) .. '|' .. cf_esc(cf_attr(attrs, 'Target'))
          elseif tag == 'CmdEvent' then
            ev = {t = cf_attr(attrs, 'Time') or '', name = cf_attr(attrs, 'CueDestination') or cf_attr(attrs, 'Name') or ''}
          elseif tag == 'RealtimeCmd' and ev then
            out[#out + 1] = 'E|' .. cf_esc(ev.t) .. '|' .. cf_esc(cf_attr(attrs, 'ExecToken') or cf_attr(attrs, 'Token'))
              .. '|' .. cf_esc(cf_attr(attrs, 'Status')) .. '|' .. cf_esc(cf_attr(attrs, 'ValCueDestination') or cf_attr(attrs, 'Cue'))
              .. '|' .. cf_esc(ev.name)
            ev = nil
          end
        end
        -- the cues in this song's sequences (CF_SEQS: names set by CueForge), to spot new ones
        for _, name in ipairs(CF_SEQS or {}) do
          local ok, kids = pcall(function() return DataPool().Sequences:Children() end)
          for _, sq in ipairs((ok and kids) or {}) do
            local ok2, sn = pcall(function() return sq.name end)
            if ok2 and sn == name then
              out[#out + 1] = 'S|' .. cf_esc(name)
              local ok3, cues = pcall(function() return sq:Children() end)
              for _, q in ipairs((ok3 and cues) or {}) do
                local ok4, no = pcall(function() return q.no end)
                local ok5, qn = pcall(function() return q.name end)
                if ok4 and tonumber(no) and tonumber(no) > 0 then
                  out[#out + 1] = 'C|' .. cf_esc(no) .. '|' .. cf_esc(ok5 and qn or '')
                end
              end
            end
          end
        end
        local s = table.concat(out, '%0A')
        local sum = 0
        for i = 1, string.len(s) do sum = (sum * 31 + string.byte(s, i)) % 2147483647 end
        local key = string.len(s) .. ':' .. sum
        if force or CF_LAST[no] ~= key then
          CF_LAST[no] = key
          local size = 130
          local n = math.max(1, math.ceil(string.len(s) / size))
          for i = 1, n do CF_PULL_SEND(line, no, i, n, string.sub(s, (i - 1) * size + 1, i * size)) end
        else
          CF_PULL_SEND(line, no, 1, 1, 'same')
        end
      end
    end
  end
end
"""

SPECIAL = ("same", "none", "noexport")


def _unesc(s: str) -> str:
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)


def parse_sequences(text: str) -> dict[str, list[tuple[float, str]]]:
    """The cue lists the console reported ('S|name' / 'C|no|name' lines): {sequence name:
    [(cue number, cue name)]}. MA may count cue numbers in thousandths (1.5 = 1500)."""
    seqs: dict[str, list[tuple[float, str]]] = {}
    cur = None
    for line in text.split("%0A"):
        parts = line.split("|")
        if parts[0] == "S" and len(parts) >= 2:
            cur = _unesc(parts[1])
            seqs[cur] = []
        elif parts[0] == "C" and len(parts) >= 2 and cur is not None:
            try:
                no = float(_unesc(parts[1]))
            except ValueError:
                continue
            seqs[cur].append((no, html.unescape(_unesc(parts[2])) if len(parts) > 2 else ""))
    for name, cues in seqs.items():
        if cues and max(n for n, _ in cues) >= 1000:
            seqs[name] = [(round(n / 1000, 3), q) for n, q in cues]
    return seqs


def console_cues(project: Project, seqs: dict, record: dict, ignored: list) -> dict:
    """Compare the console's cue lists with the current song. Returns
    {"new": [Suggestion…], "renamed": [(cue, name)…]}: cues stored on the console that
    CueForge doesn't have become suggestions (placed between their neighbours by number);
    cues renamed on the console (and not in CueForge since the last sync) get the new name.
    Cues of other songs in a shared sequence, cues CueForge pushed and later deleted, and
    suggestions you rejected are left alone."""
    from ..core.editing import effective_cue_numbers, sequence_name
    from ..core.model import Suggestion, lane_per_song
    from ..export.ma3 import in_song
    out = {"new": [], "renamed": []}
    song = project.song
    pending = {(s.lane_id, s.number) for s in project.suggestions if s.kind == "console"}
    for lane in project.lanes:
        name = sequence_name(project, lane)
        if name not in seqs:
            continue
        known: set[float] = {float(v[1]) for v in record.values() if v[0] == name}
        for other in (project.songs if not lane_per_song(lane) else [song]):
            with in_song(project, other):
                known |= {float(n) for n in effective_cue_numbers(project, lane.id).values()}
        nums = effective_cue_numbers(project, lane.id)
        mine = sorted(((nums[c.id], c) for c in project.cues_in_lane(lane.id)), key=lambda x: x[0])
        by_num = {n: c for n, c in mine if not c.duration}
        for no, qname in seqs[name]:
            if no in by_num:                                   # renamed on the console?
                c = by_num[no]
                rec = record.get(c.id)
                synced = rec[2] if rec and len(rec) > 2 else None
                if qname and qname != c.label and synced is not None and c.label == synced and qname != synced:
                    out["renamed"].append((c, qname))
                continue
            if no in known or (lane.id, no) in pending or [name, no] in ignored:
                continue
            before = [c.time for n, c in mine if n < no]
            after = [c.time for n, c in mine if n > no]
            if before and after:
                t = (before[-1] + min(after)) / 2
            elif before:
                t = before[-1] + 2.0
            else:
                t = max(0.0, (min(after) if after else 2.0) - 2.0)
            out["new"].append(Suggestion(
                kind="console", time=round(t, 3), confidence=1.0, lane_id=lane.id, label=qname, number=no,
                reason=f"Cue {no:g}{' ' + chr(39) + qname + chr(39) if qname else ''} was added on the console",
                idea="Move it to where it should fire, then accept"))
            pending.add((lane.id, no))
    return out


def parse_pull(text: str) -> TcShow:
    """The console's compact timecode (see PULL_LUA) -> TcShow, like a parsed XML file."""
    from ..export.ma3_import import _seq_from
    show = TcShow("", None)
    track: TcTrack | None = None
    for line in text.split("%0A"):                 # split first: fields escape their own % | newline
        parts = line.split("|")
        if parts[0] == "T" and len(parts) >= 2:
            track = TcTrack(html.unescape(_unesc(parts[1])), _seq_from(_unesc(parts[2]) if len(parts) > 2 else ""))
            show.tracks.append(track)
        elif parts[0] == "E" and len(parts) >= 6 and track is not None:
            t = _hms(_unesc(parts[1]).rstrip("s"))
            if t is None:
                continue
            token = _unesc(parts[2]) or "Goto"
            cue = _cue_from_handle(_unesc(parts[4]))
            track.events.append(TcEvent(t, token, _unesc(parts[3]) or "On", cue, html.unescape(_unesc(parts[5]))))
    for tr in show.tracks:
        tr.events.sort(key=lambda e: e.time)
    return show


def signature(items) -> tuple:
    """What the timecode does, for comparing versions: per track, (time, Temp hold, cue)."""
    out = {}
    for _seq, tname, c in items:
        out.setdefault((tname or "").strip().lower(), []).append(
            (round(c.time, 2), round(c.duration or 0, 2), c.number))
    return tuple(sorted((k, tuple(sorted(v, key=lambda e: (e[0], e[1], e[2] or 0)))) for k, v in out.items()))


def own_items(project: Project) -> list:
    """CueForge's current song as the console would report it (via our own XML)."""
    from ..export.ma3 import build_ma3_xml
    from ..export.ma3_import import parse_timecode_xml
    return show_to_cues(parse_timecode_xml(build_ma3_xml(project)))


def merge_into_song(project: Project, items) -> dict:
    """Bring the console's version of the current song into its lanes (tracks matched by
    sequence name, then lane name). Cues keep labels, fades and notes: normal cues are
    matched by cue number, Temps in time order. Returns counts and unmatched tracks."""
    from ..core.editing import effective_cue_numbers, sequence_name
    by_track: dict[str, list[Cue]] = {}
    for _seq, tname, c in items:
        by_track.setdefault((tname or "").strip().lower(), []).append(c)
    stats = {"moved": 0, "added": 0, "removed": 0, "unmatched": []}
    for tname, got in by_track.items():
        lane = next((l for l in project.lanes if sequence_name(project, l).lower() == tname), None) \
            or next((l for l in project.lanes if l.name.strip().lower() == tname), None)
        if lane is None:
            stats["unmatched"].append(tname)
            continue
        mine = project.cues_in_lane(lane.id)
        nums = effective_cue_numbers(project, lane.id)
        keep: set[str] = set()
        # normal cues by number
        plain = {}
        for c in mine:
            if not c.duration:
                plain.setdefault(nums[c.id], c)
        for g in (x for x in got if not x.duration):
            c = plain.pop(g.number, None) if g.number is not None else None
            if c is not None:
                if abs(c.time - g.time) > 0.002:
                    c.time = round(g.time, 6)
                    stats["moved"] += 1
                keep.add(c.id)
            else:
                new = Cue(lane_id=lane.id, time=round(g.time, 6), label=g.label, number=g.number,
                          notes="added on the console", source="manual")
                project.cues.append(new)
                keep.add(new.id)
                stats["added"] += 1
        # Temps in time order
        temps = sorted((c for c in mine if c.duration), key=lambda c: c.time)
        for k, g in enumerate(sorted((x for x in got if x.duration), key=lambda x: x.time)):
            if k < len(temps):
                c = temps[k]
                if abs(c.time - g.time) > 0.002 or abs((c.duration or 0) - g.duration) > 0.002:
                    c.time, c.duration = round(g.time, 6), round(g.duration, 3)
                    stats["moved"] += 1
                keep.add(c.id)
            else:
                new = Cue(lane_id=lane.id, time=round(g.time, 6), label="", duration=round(g.duration, 3),
                          notes="added on the console", source="manual")
                project.cues.append(new)
                keep.add(new.id)
                stats["added"] += 1
        gone = [c.id for c in mine if c.id not in keep]
        if gone:
            project.cues = [c for c in project.cues if c.id not in set(gone)]
            stats["removed"] += len(gone)
    project.sort_cues()
    return stats


class PullReceiver:
    """Listens for the console's replies (UDP, OSC) on a background thread and hands each
    complete message to `on_message(slot, text)` (called from that thread)."""

    def __init__(self, port: int, on_message) -> None:
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
        self._parts: dict[int, dict[int, str]] = {}
        self._on = on_message
        d = Dispatcher()
        d.map("/cueforge/tc", self._handle)
        self.server = ThreadingOSCUDPServer(("0.0.0.0", int(port)), d)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="ma3-pull", daemon=True).start()

    def close(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass

    def _handle(self, _addr, *args) -> None:
        if not args:
            return
        m = re.fullmatch(r"(\d+):(\d+):(\d+):(.*)", str(args[0]), re.S)
        if not m:
            return
        slot, i, n, payload = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        if i == 1:
            self._parts[slot] = {}
        parts = self._parts.setdefault(slot, {})
        parts[i] = payload
        if len(parts) == n and all(k in parts for k in range(1, n + 1)):
            text = "".join(parts[k] for k in range(1, n + 1))
            del self._parts[slot]
            self._on(slot, text)
