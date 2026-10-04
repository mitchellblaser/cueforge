"""grandMA3 export: timecode show XML and an alternative Lua plugin.

Each exported lane becomes a timecode track targeting its MA3 sequence; each cue
becomes a "Goto" event at the cue's time.

NOTE: grandMA3's XML layout is not publicly specified. The structure below follows
timecode XML exported by grandMA3 (v1.9 – v2.x). If your software version
rejects the import, use the Lua plugin export instead, which builds the
timecode show through the console's own object API and command line.
"""
from __future__ import annotations

from contextlib import contextmanager
from xml.sax.saxutils import quoteattr

from ..core.editing import effective_cue_numbers, temp_cue_label
from ..core.model import Project, Song

MA3_TICKS_PER_SECOND = 16777216  # MA3's internal 1/2^24 s (read by the importer; XML uses seconds)


def _fmt_time(seconds: float) -> str:
    """Seconds with millisecond precision, as grandMA3 writes them in timecode XML."""
    return f"{seconds:.3f}"


def _frame_readout(project: Project) -> str:
    return {"24": "24 fps", "25": "25 fps", "29.97df": "30 fps", "29.97": "30 fps", "30": "30 fps"}[
        project.frame_rate_key]


@contextmanager
def in_song(project: Project, song: Song):
    """Temporarily make `song` the current song (project.cues etc. refer to it)."""
    prev = project.song.id
    project.select_song(song.id)
    try:
        yield song
    finally:
        project.select_song(prev)


def songs_with_cues(project: Project) -> list[Song]:
    return [s for s in project.songs if any(c for c in s.cues)]


def export_lanes(project: Project) -> list:
    return [l for l in project.lanes if l.export and project.cues_in_lane(l.id)]


def seq_number(project: Project, lane) -> int:
    return lane.ma3_sequence + project.song.seq_offset


def build_ma3_xml(project: Project, timecode_number: int | None = None, duration: float | None = None,
                  placeholders: bool = False) -> str:
    """Timecode show XML for the current song, in the layout grandMA3 exports: times in
    seconds, events as RealtimeCmd key presses (ExecToken) on the track's sequence. MA3
    rejects an Offset attribute, so the song's start timecode is added to every time
    (events sit at the real show timecode, e.g. 07:00:01).

    With `placeholders`, each sequence address is written as @SEQn@ for the console to fill
    in (SEQ_FIX_LUA) — MA addresses sequences by their place in the pool, which only the
    console knows."""
    ex = project.export
    song = project.song
    name = song.name
    off = project.tc_offset
    if duration is None:
        duration = max((c.time + (c.duration or 0) for c in project.cues), default=0.0) + 5.0
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<GMA3 DataVersion="{ex.ma3_data_version}">',
             f'\t<Timecode Name={quoteattr(name)} Duration="{_fmt_time(off + duration)}" LoopCount="0" '
             f'TCSlot="-1" AutoStop="No" SwitchOff="Keep Playbacks" TimeDisplayFormat="Default" '
             f'FrameReadout="{_frame_readout(project)}">',
             '\t\t<TrackGroup Play="" Rec="">',
             '\t\t\t<MarkerTrack Name="Marker"/>']
    for lane in export_lanes(project):
        seq = seq_number(project, lane)
        nums = effective_cue_numbers(project, lane.id)
        tlabel = temp_cue_label(project, lane.id)
        lines.append(f'\t\t\t<Track Name={quoteattr(lane.name)} '
                     f'Target="ShowData.DataPools.Default.Sequences.{seq}" Play="" Rec="">')
        lines.append('\t\t\t\t<TimeRange Duration="To End" Play="" Rec="">')
        lines.append('\t\t\t\t\t<CmdSubTrack>')
        tokens = cue_tokens(project, lane.id)
        for c in project.cues_in_lane(lane.id):
            num = nums[c.id]
            if c.duration:
                # Temp: Temp On at the start, Temp Off when the hold ends (all on the lane's Temp cue)
                lines += _event(tlabel or f"Cue {num:g}", off + c.time, "Temp", "On", seq, num, placeholders)
                lines += _event(tlabel or f"Cue {num:g}", off + c.time + c.duration, "Temp", "Off", seq, num,
                                placeholders)
            else:
                lines += _event(c.label or f"Cue {num:g}", off + c.time, tokens[c.id], "On", seq, num, placeholders)
        lines.append('\t\t\t\t\t</CmdSubTrack>')
        lines.append('\t\t\t\t</TimeRange>')
        lines.append('\t\t\t</Track>')
    lines.append('\t\t</TrackGroup>')
    lines.append('\t</Timecode>')
    lines.append('</GMA3>')
    return "\n".join(lines) + "\n"


def cue_tokens(project: Project, lane_id: str) -> dict[str, str]:
    """MA3 command per normal (non-Temp) cue in a lane: Go+ by default (doesn't retrigger the
    way a Goto can), with the first cue a Goto so the sequence is in sync whenever the
    timecode starts. Temps are always Temp On / Temp Off."""
    ex = project.export
    tok = "Goto" if ex.ma3_cue_token == "Goto" else "Go+"
    out = {}
    first = True
    for c in project.cues_in_lane(lane_id):
        if c.duration:
            continue
        out[c.id] = "Goto" if (first and ex.ma3_first_goto) else tok
        first = False
    return out


def go_plus_warnings(project: Project, all_songs: bool = True) -> list[str]:
    """Go+ steps through the sequence in cue-number order, so cue numbers must rise with
    time, and Temps are best kept in a lane of their own."""
    if project.export.ma3_cue_token != "Go+":
        return []
    out = []
    for song in (project.songs if all_songs else [project.song]):
        with in_song(project, song):
            for lane in export_lanes(project):
                nums = effective_cue_numbers(project, lane.id)
                cues = project.cues_in_lane(lane.id)
                plain = [nums[c.id] for c in cues if not c.duration]
                if any(b <= a for a, b in zip(plain, plain[1:])):
                    out.append(f"{song.name} / {lane.name}: cue numbers are not in time order "
                               "(Go+ would play them in the wrong order)")
                if plain and any(c.duration for c in cues):
                    out.append(f"{song.name} / {lane.name}: mixes Temps and Go+ cues in one sequence "
                               "(put Temps in their own lane, e.g. Strobe)")
    return out


def _seq_handle(seq: int) -> str:
    """The sequence's address as MA3 writes it in timecode XML: ShowData.DataPools.Default
    .Sequences as 12.12.0.5, then the sequence's 0-based place in the pool (sequence N is
    N-1 when sequences 1…N all exist)."""
    return f"12.12.0.5.{max(0, seq - 1)}"


# Lua (no double quotes, no comments: it also travels as one `Lua "…"` command line) that
# fills @SEQn@ / @CUEn:c@ in the timecode XML with the addresses grandMA3 itself gives that
# sequence / cue (handle:AddrNative()), so nothing about the show's pool layout is guessed.
SEQ_FIX_LUA = (
    "local cf_seqs = {} "
    "local function cf_num(x) local ok, v = pcall(function() return x.no end) return ok and tonumber(v) or nil end "
    "local function cf_find(list, n) "
    "if not list then return nil end "
    "for _, o in ipairs(list) do local v = cf_num(o) "
    "if v and (math.abs(v - n) < 1e-6 or math.abs(v - n * 1000) < 1e-3) then return o end end end "
    "local function cf_kids(h) local ok, k = pcall(function() return h:Children() end) if ok then return k end end "
    "local function cf_seq(n) if cf_seqs[n] == nil then "
    "local ok, pool = pcall(function() return DataPool().Sequences end) "
    "cf_seqs[n] = (ok and pool and cf_find(cf_kids(pool), n)) or false end return cf_seqs[n] end "
    "local function cf_addr(h) local ok, a = pcall(function() return h:AddrNative() end) "
    "if ok and type(a) == 'string' and a ~= '' then return a end "
    "ok, a = pcall(function() return h:Addr() end) if ok and type(a) == 'string' then return a end end "
    "local cf_bad = {} "
    "local function cf_warn(m) if not cf_bad[m] then cf_bad[m] = true ErrPrintf('CueForge: %s', m) end end "
    "local function cf_fix(x) "
    "x = x:gsub('@SEQ(%d+)@', function(n) n = tonumber(n) local s = cf_seq(n) local a = s and cf_addr(s) "
    "if a then return a end cf_warn('Sequence ' .. n .. ' not found: push cues first, then push timecode') "
    "return 'ShowData.DataPools.Default.Sequences.' .. n end) "
    "x = x:gsub('@CUE(%d+):([%d%.]+)@', function(n, c) n, c = tonumber(n), tonumber(c) local s = cf_seq(n) "
    "local q = s and cf_find(cf_kids(s), c) local a = q and cf_addr(q) "
    "if a then return a end cf_warn('Sequence ' .. n .. ' cue ' .. c .. ' not found: push cues first') "
    "return '' end) "
    "return x end "
)


# The same on every event (the network push sends it once and expands it on the console)
RC_HEAD = '<RealtimeCmd Type="Key" Source="Original" UserProfile="0" User="1" '
RC_FLAGS = ('IsRealtime="0" IsXFade="0" IgnoreFollow="0" IgnoreCommand="0" Assert="0" IgnoreNetwork="0" '
            'FromTriggerNode="0" IgnoreExecTime="0" IssuedByTimecode="0" FromLocalHardwareFader="1" '
            'IgnoreExecXFade="0" IsExecXFade="0" ')


def _event(name: str, t: float, token: str, status: str, seq: int, cue: float,
           placeholders: bool = False) -> list[str]:
    obj = f"@SEQ{seq}@" if placeholders else _seq_handle(seq)
    dest = f"@CUE{seq}:{cue:g}@" if placeholders else f"{obj}.{int(round(cue * 1000))}"
    return [f'\t\t\t\t\t\t<CmdEvent Name={quoteattr(token)} Time="{_fmt_time(t)}" '
            f'CueDestination={quoteattr(name)}>',
            f'\t\t\t\t\t\t\t{RC_HEAD}Status="{status}" {RC_FLAGS}Object="{obj}" '
            f'ExecToken="{token}" ValCueDestination="{dest}"/>',
            '\t\t\t\t\t\t</CmdEvent>']


def export_ma3_xml(project: Project, path: str, timecode_number: int | None = None) -> int:
    """Current song -> one XML file. Returns the number of cue events."""
    xml = build_ma3_xml(project, timecode_number)
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    return sum(len(project.cues_in_lane(l.id)) for l in export_lanes(project))


def _safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in " -_()" else "_" for ch in name).strip() or "song"


def export_ma3_xml_all(project: Project, folder: str) -> list[str]:
    """One timecode XML per song (numbered in setlist order). Returns the file paths."""
    import os
    os.makedirs(folder, exist_ok=True)
    paths = []
    for i, song in enumerate(project.songs, 1):
        with in_song(project, song):
            if not export_lanes(project):
                continue
            path = os.path.join(folder, f"{i:02d} {_safe_filename(song.name)}.xml")
            export_ma3_xml(project, path)
            paths.append(path)
    return paths


def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _song_lua(project: Project, tc_number: int) -> str:
    lanes_lua = []
    for lane in export_lanes(project):
        nums = effective_cue_numbers(project, lane.id)
        toks = cue_tokens(project, lane.id)
        tlabel = temp_cue_label(project, lane.id)
        tfade = next((c.fade for c in project.cues_in_lane(lane.id) if c.duration and c.fade), None)

        def fade_of(c):
            f = tfade if c.duration else c.fade
            return f", fade={f:g}" if f else ""
        evs = ",\n".join(
            f"        {{t={c.time:.6f}, cue={nums[c.id]:g}, label={_lua_str((tlabel if c.duration else c.label) or '')}"
            + fade_of(c)
            + (f", off={c.time + c.duration:.6f}" if c.duration else f", tok={_lua_str(toks[c.id])}") + "}"
            for c in project.cues_in_lane(lane.id))
        lanes_lua.append(f"      {{name={_lua_str(lane.name)}, seq={seq_number(project, lane)}, events={{\n{evs}\n      }}}}")
    lanes_src = ",\n".join(lanes_lua)
    xml = build_ma3_xml(project, tc_number, placeholders=True)
    assert "]==]" not in xml
    return (f"  {{name={_lua_str(project.song.name)}, tc={tc_number}, offset={project.tc_offset:.6f},\n"
            f"    xml=[==[{xml}]==],\n    lanes={{\n{lanes_src}\n    }}}}")


def build_ma3_lua(project: Project, timecode_number: int | None = None, create_cues: bool = True,
                  all_songs: bool = True) -> str:
    """One grandMA3 plugin that does everything for the whole setlist when run:

    1. creates (empty, labelled) cues in the target sequences — plain command line;
    2. for every song, writes its timecode XML (embedded below) into the MA3 timecode
       library and runs the console's own `Import Timecode` into the song's slot;
    3. if that import is not possible, builds the timecode show through the Lua object API.
    """
    if all_songs:
        songs_src = []
        for song in project.songs:
            with in_song(project, song):
                if export_lanes(project):
                    songs_src.append(_song_lua(project, song.ma3_timecode))
        title = project.name
    else:
        songs_src = [_song_lua(project, timecode_number or project.song.ma3_timecode)]
        title = project.song.name
    songs = ",\n".join(songs_src)
    return f'''-- CueForge export: {title}
-- Copy this .lua and its .xml into your MA3 plugin library (gma3_library/datapools/plugins),
-- import the plugin into a Plugin pool slot and run it once. It creates the cues and
-- imports every song's timecode show into its own Timecode slot. No separate XML import.
local CREATE_CUES = {"true" if create_cues else "false"}
local SONGS = {{
{songs}
}}

local function q(s) return (s:gsub('"', "'")) end

local function create_cues(song)
  for _, lane in ipairs(song.lanes) do
    local made = {{}}                       -- Temps all fire one cue: store it once
    for _, ev in ipairs(lane.events) do
      local addr = "Sequence " .. lane.seq .. " Cue " .. ev.cue
      if not made[addr] then
        made[addr] = true
        Cmd("Store " .. addr .. " /Merge /NoConfirm")
        if ev.label ~= "" then Cmd("Label " .. addr .. ' "' .. q(ev.label) .. '"') end
        if ev.fade then Cmd(addr .. " CueFade " .. ev.fade) end
      end
    end
  end
end

-- Where the console imports timecode XML from (gma3_library/datapools/timecodes)
local function timecode_dirs()
  local dirs = {{}}
  local function add(fn)
    local ok, p = pcall(fn)
    if ok and type(p) == "string" and p ~= "" then dirs[#dirs + 1] = p end
  end
  add(function() return GetPath(Enums.PathType.Library) .. "/datapools/timecodes" end)
  add(function() return GetPath("library", true) .. "/datapools/timecodes" end)
  add(function() return GetPath(Enums.PathType.UserTimecodes) end)
  return dirs
end

{SEQ_FIX_LUA.replace("{", "{{").replace("}", "}}")}

local function import_xml(song)
  for _, dir in ipairs(timecode_dirs()) do
    local fname = "CueForge_" .. song.tc .. ".xml"
    local f = io.open(dir .. "/" .. fname, "w")
    if f then
      f:write(cf_fix(song.xml))     -- each sequence's real place in this show's pool
      f:close()
      Cmd("Delete Timecode " .. song.tc .. " /NoConfirm")
      Cmd("Import Timecode " .. song.tc .. ' /File "' .. fname .. '" /NoConfirm')
      if DataPool().Timecodes[song.tc] then
        Printf("CueForge: imported '%s' into Timecode %d", song.name, song.tc)
        return true
      end
    end
  end
  return false
end

-- Fallback: build the show with the object API
local function build_via_api(song)
  Cmd("Store Timecode " .. song.tc .. " /Overwrite /NoConfirm")
  Cmd("Label Timecode " .. song.tc .. ' "' .. q(song.name) .. '"')
  local tc = DataPool().Timecodes[song.tc]
  if not tc then
    ErrPrintf("CueForge: could not create Timecode %d", song.tc)
    return
  end
  tc.offsettcslot = song.offset
  local group = tc:Acquire()
  for _, lane in ipairs(song.lanes) do
    local seq = DataPool().Sequences[lane.seq]
    if not seq then
      ErrPrintf("CueForge: Sequence %d not found, skipping lane %s", lane.seq, lane.name)
    else
      local track = group:Acquire()
      track.name = lane.name
      track.target = seq
      local sub = track:Acquire():Acquire()
      for _, ev in ipairs(lane.events) do
        local cue = seq:Find("Cue " .. ev.cue) or seq[ev.cue + 1]
        local e = sub:Acquire()
        e.time = ev.t
        if cue then e.cue = cue end
        if ev.label ~= "" then e.name = ev.label end
        if ev.off then
          e.token = "Temp"
          e.status = "On"
          local o = sub:Acquire()
          o.time = ev.off
          o.token = "Temp"
          o.status = "Off"
          if cue then o.cue = cue end
        else
          e.token = ev.tok
        end
      end
    end
  end
  Printf("CueForge: built '%s' in Timecode %d", song.name, song.tc)
end

local function main()
  local names = {{}}
  for _, song in ipairs(SONGS) do names[#names + 1] = song.name .. " -> Timecode " .. song.tc end
  if not Confirm("CueForge", "Create cues and timecode for " .. #SONGS .. " song(s)?\\n" ..
                 table.concat(names, "\\n")) then
    return
  end
  Cmd("CmdDelay 0")
  for _, song in ipairs(SONGS) do
    if CREATE_CUES then create_cues(song) end
    if not import_xml(song) then build_via_api(song) end
  end
  Printf("CueForge: done (%d songs)", #SONGS)
end

return main
'''


def build_ma3_plugin_xml(project: Project, lua_filename: str) -> str:
    """Plugin descriptor that grandMA3 imports into the Plugin pool (references the .lua file)."""
    name = project.name
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<GMA3 DataVersion="{project.export.ma3_data_version}">\n'
            f'\t<Plugin Name={quoteattr("CueForge " + name)} Version="1.0.0">\n'
            f'\t\t<ComponentLua Name={quoteattr("CueForge " + name)} FileName={quoteattr(lua_filename)}/>\n'
            '\t</Plugin>\n</GMA3>\n')


def export_ma3_lua(project: Project, path: str, timecode_number: int | None = None, create_cues: bool = True,
                   all_songs: bool = True) -> str:
    """Write the .lua plugin and its .xml descriptor next to it. Returns the descriptor path."""
    import os
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_ma3_lua(project, timecode_number, create_cues, all_songs))
    xml_path = os.path.splitext(path)[0] + ".xml"
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(build_ma3_plugin_xml(project, os.path.basename(path)))
    return xml_path


def build_ma3_macro_commands(project: Project, all_songs: bool = False) -> list[str]:
    """Plain command lines to create and label the planned cues (no timecode)."""
    cmds = []
    for song in (project.songs if all_songs else [project.song]):
        with in_song(project, song):
            for lane in export_lanes(project):
                nums = effective_cue_numbers(project, lane.id)
                tlabel = temp_cue_label(project, lane.id)
                tfade = next((c.fade for c in project.cues_in_lane(lane.id) if c.duration and c.fade), None)
                made = set()
                for c in project.cues_in_lane(lane.id):
                    addr = f"Sequence {seq_number(project, lane)} Cue {nums[c.id]:g}"
                    if addr in made:                   # Temps share one cue
                        continue
                    made.add(addr)
                    cmds.append(f"Store {addr} /Merge /NoConfirm")
                    label = tlabel if c.duration else c.label
                    if label:
                        cmds.append(f'Label {addr} "{label.replace(chr(34), chr(39))}"')
                    fade = tfade if c.duration else c.fade
                    if fade:
                        cmds.append(f"{addr} CueFade {fade:g}")
    return cmds


def cue_number_clashes(project: Project) -> list[str]:
    """Songs that would write the same cue number into the same sequence."""
    seen: dict[tuple[int, float], str] = {}
    clashes = []
    for song in project.songs:
        with in_song(project, song):
            for lane in export_lanes(project):
                seq = seq_number(project, lane)
                for num in effective_cue_numbers(project, lane.id).values():
                    other = seen.get((seq, num))
                    if other and other != song.name:
                        clashes.append(f"Sequence {seq} cue {num:g}: '{other}' and '{song.name}'")
                    seen[(seq, num)] = song.name
    return clashes
