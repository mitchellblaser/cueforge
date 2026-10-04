"""Control actions (shared by MIDI and OSC) and their mapping model."""
from __future__ import annotations

import colorsys
from dataclasses import asdict, dataclass, field

# action id -> human description. "{n}" actions take a 1-based lane number.
ACTIONS = {
    "cue:lane:{n}": "Cue into lane {n}",
    "temp:lane:{n}": "Temp into lane {n}",
    "cue:active": "Cue into the active lane",
    "temp:active": "Temp into the active lane",
    "transport:toggle": "Play / pause",
    "transport:stop": "Stop and go to start",
    "lane:next": "Next lane (active)",
    "lane:prev": "Previous lane (active)",
    "sugg:next": "Next AI suggestion",
    "sugg:prev": "Previous AI suggestion",
    "sugg:accept": "Accept suggestion",
    "sugg:reject": "Reject suggestion",
    "edit:undo": "Undo",
    "section:add": "Add section marker",
    "song:next": "Next song",
    "song:prev": "Previous song",
    "loop:toggle": "Loop on/off",
}


def action_label(action: str) -> str:
    if action.startswith(("cue:lane:", "temp:lane:")):
        n = action.rsplit(":", 1)[1]
        return ACTIONS[action.rsplit(":", 1)[0] + ":{n}"].replace("{n}", n)
    return ACTIONS.get(action, action)


def all_actions(n_lanes: int = 8) -> list[str]:
    out = []
    for k in ACTIONS:
        if "{n}" in k:
            out += [k.replace("{n}", str(i)) for i in range(1, n_lanes + 1)]
        else:
            out.append(k)
    return out


@dataclass
class MidiMapping:
    kind: str          # "note" | "cc"
    channel: int       # 0-15, or -1 = any channel (pad controllers send on all sorts of channels)
    number: int        # note or controller number
    action: str

    def key(self) -> tuple:
        return (self.kind, self.channel, self.number)

    def matches(self, kind: str, channel: int, number: int) -> bool:
        return self.kind == kind and self.number == number and self.channel in (-1, channel)

    def describe(self) -> str:
        what = "Note" if self.kind == "note" else "CC"
        return f"{what} {self.number} · " + ("any ch" if self.channel < 0 else f"ch {self.channel + 1}")


def default_midi_map() -> list[MidiMapping]:
    """Two rows of 8 pads (typical pad controller, notes 36-51): top row Temps, bottom
    row Cues, lanes 1-8."""
    m = [MidiMapping("note", -1, 36 + i, f"cue:lane:{i + 1}") for i in range(8)]
    m += [MidiMapping("note", -1, 44 + i, f"temp:lane:{i + 1}") for i in range(8)]
    return m


def _old_default_map() -> list[dict]:
    m = [MidiMapping("note", 0, 36 + i, f"cue:lane:{i + 1}") for i in range(8)]
    m += [MidiMapping("note", 0, 44 + i, f"temp:lane:{i + 1}") for i in range(8)]
    return [asdict(x) for x in m]


@dataclass
class ControlSettings:
    midi_in: str = ""
    midi_out: str = ""
    feedback: str = "auto"        # "auto" (by device) | "palette" (Launchpad/APC style) | "midifighter" | "onoff" | "off"
    feedback_channel: int = -1    # -1 = the channel the device sends on
    hold_from_press: bool = False  # Temp hold = how long the pad/button is held
    midi_map: list[dict] = field(default_factory=lambda: [asdict(m) for m in default_midi_map()])
    osc_enabled: bool = False
    osc_port: int = 8100         # not 8000: grandMA3 often uses 8000 on the same machine
    osc_feedback_host: str = "127.0.0.1"
    osc_feedback_port: int = 9000

    def mappings(self) -> list[MidiMapping]:
        if self.midi_map == _old_default_map():   # settings saved before "any channel" existed
            self.midi_map = [asdict(m) for m in default_midi_map()]
        return [MidiMapping(**m) for m in self.midi_map]


# ------------------------------------------------------------------ colour feedback
def _palette() -> list[tuple[int, int, int]]:
    """Approximation of the common 128-colour pad palette used by Novation Launchpad /
    Akai APC mini mk2 style controllers: index 0 off, 1-3 greys/white, then groups of
    four (light, full, dim, dark) around the colour wheel."""
    pal = [(0, 0, 0), (64, 64, 64), (128, 128, 128), (255, 255, 255)]
    hues = [0.0, 0.07, 0.14, 0.2, 0.28, 0.33, 0.4, 0.45, 0.5, 0.55, 0.6, 0.66, 0.75, 0.83, 0.9]
    for h in hues:
        for sat, val in ((0.45, 1.0), (1.0, 1.0), (1.0, 0.45), (1.0, 0.2)):
            r, g, b = colorsys.hsv_to_rgb(h, sat, val)
            pal.append((int(r * 255), int(g * 255), int(b * 255)))
    while len(pal) < 128:
        pal.append((0, 0, 0))
    return pal


PALETTE = _palette()
WHITE = 3


def midifighter_velocity(hex_color: str, dim: bool = False) -> int:
    """Approximate Midi Fighter (Spectra / 3D) colour for a lane colour: its LED colour is set
    by note-on velocity, roughly around the colour wheel. Approximate - use On / off if the
    colours come out wrong on your unit."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 127
    hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
    if sat < 0.2:
        return 127                                  # white-ish
    v = 1 + int(round(hue * 125)) % 126
    return max(1, v // 2) if dim else v


def color_velocity(hex_color: str, dim: bool = False) -> int:
    """Nearest palette index for a lane colour (dim -> the dimmer shade of that hue)."""
    h = hex_color.lstrip("#")
    try:
        rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return WHITE
    best, bd = WHITE, 1e9
    for i in range(4, 64):
        if (i - 4) % 4 != 1:          # match on the full-brightness entries
            continue
        d = sum((a - b) ** 2 for a, b in zip(rgb, PALETTE[i]))
        if d < bd:
            best, bd = i, d
    return best + 1 if dim else best
