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
from xml.sax.saxutils import quoteattr

from ..core.editing import effective_cue_numbers
from ..core.model import Project

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


def export_lanes(project: Project) -> list:
    return [l for l in project.lanes if l.export and project.cues_in_lane(l.id)]


def build_ma3_xml(project: Project, timecode_number: int = 1, duration: float | None = None) -> str:
    ex = project.export
    unit = ex.ma3_time_unit
    name = ex.ma3_name or project.name
    if duration is None:
        duration = max((c.time for c in project.cues), default=0.0) + 5.0
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<GMA3 DataVersion="{ex.ma3_data_version}">',
             f'\t<Timecode Name={quoteattr(name)} Guid="{_guid()}" Cursor="0" LoopCount="0" '
             f'TCSlot="-1" SwitchOff="Keep Playbacks" Duration="{_fmt_time(duration, unit)}" '
             f'Offset="{_fmt_time(project.tc_offset, unit)}" TimeDisplayFormat="&lt;Auto&gt;" '
             f'FrameReadout="{_frame_readout(project)}">',
             f'\t\t<TrackGroup Name={quoteattr(name)} Guid="{_guid()}">',
             f'\t\t\t<MarkerTrack Name="Marker" Guid="{_guid()}"/>']
    for lane in export_lanes(project):
        seq = lane.ma3_sequence
        nums = effective_cue_numbers(project, lane.id)
        lines.append(f'\t\t\t<Track Name={quoteattr(lane.name)} Guid="{_guid()}" '
                     f'Target="ShowData.DataPools.Default.Sequences.{seq}">')
        lines.append(f'\t\t\t\t<TimeRange Guid="{_guid()}" Duration="{_fmt_time(duration, unit)}">')
        lines.append(f'\t\t\t\t\t<CmdSubTrack Guid="{_guid()}">')
        for c in project.cues_in_lane(lane.id):
            num = nums[c.id]
            label = c.label or f"Cue {num:g}"
            lines.append(
                f'\t\t\t\t\t\t<CmdEvent Name={quoteattr(label)} Guid="{_guid()}" Time="{_fmt_time(c.time, unit)}">')
            lines.append(
                f'\t\t\t\t\t\t\t<RealtimeCmd Type="Key" Source="Original" UserProfile="0" Status="On" '
                f'Token="Goto" Cue="ShowData.DataPools.Default.Sequences.{seq}.Cues.{num:g}"/>')
            lines.append('\t\t\t\t\t\t</CmdEvent>')
        lines.append('\t\t\t\t\t</CmdSubTrack>')
        lines.append('\t\t\t\t</TimeRange>')
        lines.append('\t\t\t</Track>')
    lines.append('\t\t</TrackGroup>')
    lines.append('\t</Timecode>')
    lines.append('</GMA3>')
    return "\n".join(lines) + "\n"


def export_ma3_xml(project: Project, path: str, timecode_number: int = 1) -> int:
    xml = build_ma3_xml(project, timecode_number)
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    return sum(len(project.cues_in_lane(l.id)) for l in export_lanes(project))


def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def build_ma3_lua(project: Project, timecode_number: int = 1, create_cues: bool = True) -> str:
    """A grandMA3 Lua plugin that (optionally) creates labelled empty cues and the
    timecode show. Cue creation uses plain command-line syntax; the timecode
    events use the object API (Acquire / property set)."""
    name = project.export.ma3_name or project.name
    lanes_lua = []
    for lane in export_lanes(project):
        nums = effective_cue_numbers(project, lane.id)
        evs = ",\n".join(
            f"      {{t={c.time:.6f}, cue={nums[c.id]:g}, label={_lua_str(c.label or '')}}}"
            for c in project.cues_in_lane(lane.id))
        lanes_lua.append(f"  {{name={_lua_str(lane.name)}, seq={lane.ma3_sequence}, events={{\n{evs}\n    }}}}")
    lanes_src = ",\n".join(lanes_lua)
    return f'''-- CueForge export: {name}
-- Import: copy this file into your MA3 plugin library, import it into a plugin
-- pool slot and run it. It writes to Timecode {timecode_number}.
local TC_NUMBER = {timecode_number}
local TC_NAME = {_lua_str(name)}
local CREATE_CUES = {"true" if create_cues else "false"}
local TC_OFFSET = {project.tc_offset:.6f}
local LANES = {{
{lanes_src}
}}

local function main()
  if not Confirm("CueForge", "Write timecode show '" .. TC_NAME .. "' into Timecode " .. TC_NUMBER .. "?") then
    return
  end
  Cmd("CmdDelay 0")
  if CREATE_CUES then
    for _, lane in ipairs(LANES) do
      for _, ev in ipairs(lane.events) do
        local addr = "Sequence " .. lane.seq .. " Cue " .. ev.cue
        Cmd("Store " .. addr .. " /Merge /NoConfirm")
        if ev.label ~= "" then
          Cmd("Label " .. addr .. ' "' .. ev.label:gsub('"', "'") .. '"')
        end
      end
    end
  end
  Cmd("Store Timecode " .. TC_NUMBER .. " /Overwrite /NoConfirm")
  Cmd("Label Timecode " .. TC_NUMBER .. ' "' .. TC_NAME .. '"')
  local tc = DataPool().Timecodes[TC_NUMBER]
  if not tc then
    ErrPrintf("CueForge: could not create Timecode %d", TC_NUMBER)
    return
  end
  tc.offsettcslot = TC_OFFSET
  local group = tc:Acquire()
  for _, lane in ipairs(LANES) do
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
        e.token = "Goto"
        if cue then e.cue = cue end
        if ev.label ~= "" then e.name = ev.label end
      end
    end
  end
  Printf("CueForge: timecode show written to Timecode %d", TC_NUMBER)
end

return main
'''


def build_ma3_plugin_xml(project: Project, lua_filename: str) -> str:
    """Plugin descriptor that grandMA3 imports into the Plugin pool (references the .lua file)."""
    name = project.export.ma3_name or project.name
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<GMA3 DataVersion="{project.export.ma3_data_version}">\n'
            f'\t<Plugin Name={quoteattr("CueForge " + name)} Version="1.0.0">\n'
            f'\t\t<ComponentLua Name={quoteattr("CueForge " + name)} FileName={quoteattr(lua_filename)}/>\n'
            '\t</Plugin>\n</GMA3>\n')


def export_ma3_lua(project: Project, path: str, timecode_number: int = 1, create_cues: bool = True) -> str:
    """Write the .lua plugin and its .xml descriptor next to it. Returns the descriptor path."""
    import os
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_ma3_lua(project, timecode_number, create_cues))
    xml_path = os.path.splitext(path)[0] + ".xml"
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(build_ma3_plugin_xml(project, os.path.basename(path)))
    return xml_path


def build_ma3_macro_commands(project: Project) -> list[str]:
    """Plain command lines to create and label the planned cues (no timecode)."""
    cmds = []
    for lane in export_lanes(project):
        nums = effective_cue_numbers(project, lane.id)
        for c in project.cues_in_lane(lane.id):
            addr = f"Sequence {lane.ma3_sequence} Cue {nums[c.id]:g}"
            cmds.append(f"Store {addr} /Merge /NoConfirm")
            if c.label:
                cmds.append(f'Label {addr} "{c.label.replace(chr(34), chr(39))}"')
    return cmds
