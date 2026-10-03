"""grandMA3 export: timecode show XML and an alternative Lua plugin.

Each exported lane becomes a timecode track targeting its MA3 sequence; each cue
becomes a "Goto" event at the cue's time.

NOTE: grandMA3's XML layout is not publicly specified. The structure below follows
timecode XML exported by grandMA3 (v1.9 – v2.x). If your software version
rejects the import, use the Lua plugin export instead, which builds the
timecode show through the console's own object API and command line.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from xml.sax.saxutils import quoteattr

from ..core.editing import effective_cue_numbers
from ..core.model import Project, Song

MA3_TICKS_PER_SECOND = 16777216  # MA3 stores times as 1/2^24 s


def _guid() -> str:
    h = uuid.uuid4().hex.upper()
    return " ".join(h[i:i + 2] for i in range(0, 32, 2))


def _fmt_time(seconds: float, unit: str) -> str:
    if unit == "seconds":
        return f"{seconds:.6f}".rstrip("0").rstrip(".") or "0"
    return str(int(round(seconds * MA3_TICKS_PER_SECOND)))


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


def build_ma3_xml(project: Project, timecode_number: int | None = None, duration: float | None = None) -> str:
    """Timecode show XML for the current song."""
    ex = project.export
    unit = ex.ma3_time_unit
    song = project.song
    name = song.name
    if duration is None:
        duration = max((c.time + (c.duration or 0) for c in project.cues), default=0.0) + 5.0
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<GMA3 DataVersion="{ex.ma3_data_version}">',
             f'\t<Timecode Name={quoteattr(name)} Guid="{_guid()}" Cursor="0" LoopCount="0" '
             f'TCSlot="-1" SwitchOff="Keep Playbacks" Duration="{_fmt_time(duration, unit)}" '
             f'Offset="{_fmt_time(project.tc_offset, unit)}" TimeDisplayFormat="&lt;Auto&gt;" '
             f'FrameReadout="{_frame_readout(project)}">',
             f'\t\t<TrackGroup Name={quoteattr(name)} Guid="{_guid()}">',
             f'\t\t\t<MarkerTrack Name="Marker" Guid="{_guid()}"/>']
    for lane in export_lanes(project):
        seq = seq_number(project, lane)
        nums = effective_cue_numbers(project, lane.id)
        lines.append(f'\t\t\t<Track Name={quoteattr(lane.name)} Guid="{_guid()}" '
                     f'Target="ShowData.DataPools.Default.Sequences.{seq}">')
        lines.append(f'\t\t\t\t<TimeRange Guid="{_guid()}" Duration="{_fmt_time(duration, unit)}">')
        lines.append(f'\t\t\t\t\t<CmdSubTrack Guid="{_guid()}">')
        for c in project.cues_in_lane(lane.id):
            num = nums[c.id]
            label = c.label or f"Cue {num:g}"
            cue_ref = f"ShowData.DataPools.Default.Sequences.{seq}.Cues.{num:g}"
            if c.duration:
                # Temp: Temp On at the start, Temp Off when the hold ends
                lines += _event(label, c.time, unit, "Temp", "On", cue_ref)
                lines += _event(label + " (release)", c.time + c.duration, unit, "Temp", "Off", cue_ref)
            else:
                lines += _event(label, c.time, unit, "Goto", "On", cue_ref)
        lines.append('\t\t\t\t\t</CmdSubTrack>')
        lines.append('\t\t\t\t</TimeRange>')
        lines.append('\t\t\t</Track>')
    lines.append('\t\t</TrackGroup>')
    lines.append('\t</Timecode>')
    lines.append('</GMA3>')
    return "\n".join(lines) + "\n"


def _event(name: str, t: float, unit: str, token: str, status: str, cue_ref: str) -> list[str]:
    return [f'\t\t\t\t\t\t<CmdEvent Name={quoteattr(name)} Guid="{_guid()}" Time="{_fmt_time(t, unit)}">',
            f'\t\t\t\t\t\t\t<RealtimeCmd Type="Key" Source="Original" UserProfile="0" Status="{status}" '
            f'Token="{token}" Cue="{cue_ref}"/>',
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
        evs = ",\n".join(
            f"        {{t={c.time:.6f}, cue={nums[c.id]:g}, label={_lua_str(c.label or '')}"
            + (f", off={c.time + c.duration:.6f}" if c.duration else "") + "}"
            for c in project.cues_in_lane(lane.id))
        lanes_lua.append(f"      {{name={_lua_str(lane.name)}, seq={seq_number(project, lane)}, events={{\n{evs}\n      }}}}")
    lanes_src = ",\n".join(lanes_lua)
    return (f"  {{name={_lua_str(project.song.name)}, tc={tc_number}, offset={project.tc_offset:.6f}, lanes={{\n"
            f"{lanes_src}\n    }}}}")


def build_ma3_lua(project: Project, timecode_number: int | None = None, create_cues: bool = True,
                  all_songs: bool = False) -> str:
    """A grandMA3 Lua plugin that (optionally) creates labelled empty cues and builds one
    timecode show per song. Cue creation uses plain command-line syntax; the timecode
    events use the object API (Acquire / property set)."""
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
-- Import: copy this file (and its .xml) into your MA3 plugin library, import it into a
-- plugin pool slot and run it. Each song is written to its own Timecode pool slot.
local CREATE_CUES = {"true" if create_cues else "false"}
local SONGS = {{
{songs}
}}

local function write_song(song)
  if CREATE_CUES then
    for _, lane in ipairs(song.lanes) do
      for _, ev in ipairs(lane.events) do
        local addr = "Sequence " .. lane.seq .. " Cue " .. ev.cue
        Cmd("Store " .. addr .. " /Merge /NoConfirm")
        if ev.label ~= "" then
          Cmd("Label " .. addr .. ' "' .. ev.label:gsub('"', "'") .. '"')
        end
      end
    end
  end
  Cmd("Store Timecode " .. song.tc .. " /Overwrite /NoConfirm")
  Cmd("Label Timecode " .. song.tc .. ' "' .. song.name:gsub('"', "'") .. '"')
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
      local range = track:Acquire()
      local sub = range:Acquire()
      for _, ev in ipairs(lane.events) do
        local cue = seq:Find("Cue " .. ev.cue) or seq[ev.cue + 1]
        local e = sub:Acquire()
        e.time = ev.t
        if cue then e.cue = cue end
        if ev.label ~= "" then e.name = ev.label end
        if ev.off then
          -- Temp: Temp On now, Temp Off when the hold ends
          e.token = "Temp"
          e.status = "On"
          local o = sub:Acquire()
          o.time = ev.off
          o.token = "Temp"
          o.status = "Off"
          if cue then o.cue = cue end
        else
          e.token = "Goto"
        end
      end
    end
  end
  Printf("CueForge: '%s' written to Timecode %d", song.name, song.tc)
end

local function main()
  local names = {{}}
  for _, song in ipairs(SONGS) do names[#names + 1] = song.name .. " -> Timecode " .. song.tc end
  if not Confirm("CueForge", "Write " .. #SONGS .. " timecode show(s)?\\n" .. table.concat(names, "\\n")) then
    return
  end
  Cmd("CmdDelay 0")
  for _, song in ipairs(SONGS) do write_song(song) end
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
                   all_songs: bool = False) -> str:
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
                for c in project.cues_in_lane(lane.id):
                    addr = f"Sequence {seq_number(project, lane)} Cue {nums[c.id]:g}"
                    cmds.append(f"Store {addr} /Merge /NoConfirm")
                    if c.label:
                        cmds.append(f'Label {addr} "{c.label.replace(chr(34), chr(39))}"')
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
