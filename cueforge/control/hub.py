"""MIDI + OSC control: hardware taps, transport and review, with colour feedback.

Input arrives on background threads; each hit is timestamped with the heard playback
position right away, then handed to the UI thread through a queued Qt signal.
"""
from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .actions import WHITE, ControlSettings, MidiMapping, color_velocity, midifighter_velocity

MIN_HOLD = 0.05


def match_port(saved: str, names: list[str]) -> str | None:
    """The port `saved` refers to, even if the OS renumbered it since ("Midi Fighter Spectra 0"
    -> "Midi Fighter Spectra 1", or ":24:0" client numbers on Linux)."""
    if not saved:
        return None
    if saved in names:
        return saved
    import re

    def base(n: str) -> str:
        n = re.sub(r"\s+\d+:\d+$", "", n)            # ALSA "Name 24:0"
        n = re.sub(r"[\s:]+\d+$", "", n)             # Windows "Name 1"
        return n.strip().lower()
    want = base(saved)
    hits = [n for n in names if base(n) == want] or [n for n in names if want and want in n.lower()]
    return hits[0] if hits else None


class ControlHub(QObject):
    _event = Signal(str, bool, float)      # action, pressed, song time  (cross-thread -> UI)
    _learn = Signal(object)
    learned = Signal(object)               # MidiMapping (without action) from MIDI learn
    status = Signal(str)

    def __init__(self, session, callbacks: dict[str, Callable[[], None]] | None = None) -> None:
        super().__init__()
        self.s = session
        self.callbacks = callbacks or {}
        self.cfg = self._load()
        self._in = None
        self._out = None
        self._osc_server = None
        self._osc_thread = None
        self._osc_client = None
        self._learning = False
        self._pending: dict[str, tuple[str, float]] = {}   # action -> (cue id, press time) for press-length holds
        self._event.connect(self._dispatch)
        self._learn.connect(self.learned.emit)
        for sig in (session.lanes_changed, session.active_lane_changed, session.project_replaced):
            sig.connect(self.send_feedback)
        self._tc_timer = QTimer(self)
        self._tc_timer.setInterval(100)
        self._tc_timer.timeout.connect(self._send_tc)
        self._tc_timer.start()

    # ------------------------------------------------------------------ settings
    def _load(self) -> ControlSettings:
        d = self.s.settings.get("control", {}) or {}
        try:
            return ControlSettings(**{k: v for k, v in d.items() if k in ControlSettings.__dataclass_fields__})
        except TypeError:
            return ControlSettings()

    def save(self) -> None:
        self.s.settings.set("control", asdict(self.cfg))

    def apply(self) -> None:
        """(Re)open ports / servers from the current settings."""
        self.close()
        self.open_midi()
        if self.cfg.osc_enabled:
            self.open_osc()
        self.send_feedback()

    # ------------------------------------------------------------------ MIDI
    def _refresh_feedback_later(self) -> None:
        """Called on the MIDI thread: the UI thread re-sends the pad colours on its next event."""
        self._fb_needed = True

    @staticmethod
    def midi_ports() -> tuple[list[str], list[str]]:
        try:
            import mido
            return mido.get_input_names(), mido.get_output_names()
        except Exception:
            return [], []

    def open_midi(self) -> None:
        try:
            import mido
        except Exception:
            return
        ins, outs = self.midi_ports()
        self._in_name = self._out_name = ""
        if self.cfg.midi_in:
            name = match_port(self.cfg.midi_in, ins) or self.cfg.midi_in
            try:
                self._in = mido.open_input(name, callback=self._on_midi)
                self._in_name = name
                self.status.emit(f"MIDI in: {name}")
            except Exception as exc:
                self.status.emit(f"MIDI in failed ({self.cfg.midi_in}): {exc}")
        out_wanted = self.cfg.midi_out or (self._in_name and match_port(self._in_name, outs)) or ""
        if out_wanted:                             # no output chosen: the input device's own output
            name = match_port(out_wanted, outs) or out_wanted
            try:
                self._out = mido.open_output(name)
                self._out_name = name
            except Exception as exc:
                self.status.emit(f"MIDI out failed ({self.cfg.midi_out}): {exc}")

    def _on_midi(self, msg) -> None:            # rtmidi thread
        self.handle_midi(msg, self.s.engine.position())

    def is_midifighter(self) -> bool:
        names = (getattr(self, "_in_name", "") or self.cfg.midi_in) + " " + \
            (getattr(self, "_out_name", "") or self.cfg.midi_out)
        return "midi fighter" in names.lower() or "midifighter" in names.lower()

    def feedback_mode(self) -> str:
        if self.cfg.feedback != "auto":
            return self.cfg.feedback
        return "midifighter" if self.is_midifighter() else "palette"

    def feedback_channel(self, m) -> int:
        """Channel to light a pad on: the mapping's own, else the configured one, else the
        channel the device was last heard on (Midi Fighters use channel 3)."""
        if m.channel >= 0:
            return m.channel
        if self.cfg.feedback_channel >= 0:
            return self.cfg.feedback_channel
        seen = getattr(self, "_seen_channel", None)
        if seen is not None:
            return seen
        return 2 if self.is_midifighter() else 0

    def handle_midi(self, msg, t: float) -> None:
        """Map one MIDI message (thread-safe; also used by tests)."""
        kind = {"note_on": "note", "note_off": "note", "control_change": "cc"}.get(msg.type)
        if kind is None:
            return
        number = msg.note if kind == "note" else msg.control
        if getattr(self, "_seen_channel", None) != msg.channel:
            self._seen_channel = msg.channel          # light pads back on the device's channel
            self._refresh_feedback_later()
        if kind == "note":
            pressed = msg.type == "note_on" and msg.velocity > 0
        else:
            pressed = msg.value >= 64
        if self._learning:
            if pressed:
                self._learning = False
                self._learn.emit(MidiMapping(kind, msg.channel, number, ""))
            return
        for m in self.cfg.mappings():
            if m.matches(kind, msg.channel, number):
                self._event.emit(m.action, pressed, t)

    def start_learn(self) -> None:
        self._learning = True

    def stop_learn(self) -> None:
        self._learning = False

    # ------------------------------------------------------------------ OSC
    def open_osc(self) -> None:
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
            from pythonosc.udp_client import SimpleUDPClient
        except Exception:
            self.status.emit("OSC needs python-osc (pip install python-osc)")
            return
        disp = Dispatcher()
        disp.set_default_handler(self._on_osc)
        try:
            self._osc_server = ThreadingOSCUDPServer(("0.0.0.0", int(self.cfg.osc_port)), disp)
        except OSError as exc:
            self.status.emit(f"OSC port {self.cfg.osc_port}: {exc}")
            self._osc_server = None
            return
        self._osc_thread = threading.Thread(target=self._osc_server.serve_forever, daemon=True)
        self._osc_thread.start()
        if self.cfg.osc_feedback_host and self.cfg.osc_feedback_port:
            self._osc_client = SimpleUDPClient(self.cfg.osc_feedback_host, int(self.cfg.osc_feedback_port))
        self.status.emit(f"OSC listening on port {self.cfg.osc_port}")

    @property
    def osc_port(self) -> int | None:
        return self._osc_server.server_address[1] if self._osc_server else None

    def _on_osc(self, address: str, *args) -> None:     # server thread
        self.handle_osc(address, args, self.s.engine.position())

    def handle_osc(self, address: str, args: tuple, t: float) -> None:
        parts = [p for p in address.lower().split("/") if p]
        if not parts or parts[0] != "cueforge":
            return
        parts = parts[1:]
        # integers are lane numbers, floats are button values (1 = press, 0 = release),
        # as sent by TouchOSC / Open Stage Control buttons
        ints = [a for a in args if isinstance(a, int) and not isinstance(a, bool)]
        floats = [a for a in args if isinstance(a, float)]
        press = not floats or floats[-1] > 0.5
        action = None
        if len(parts) == 3 and parts[0] == "lane" and parts[1].isdigit() and parts[2] in ("cue", "temp"):
            action = f"{parts[2]}:lane:{int(parts[1])}"
        elif len(parts) == 1 and parts[0] in ("cue", "temp"):
            action = f"{parts[0]}:lane:{ints[0]}" if ints and ints[0] >= 1 else f"{parts[0]}:active"
        else:
            simple = {("play",): "transport:toggle", ("toggle",): "transport:toggle", ("stop",): "transport:stop",
                      ("lane", "next"): "lane:next", ("lane", "prev"): "lane:prev",
                      ("suggestion", "next"): "sugg:next", ("suggestion", "prev"): "sugg:prev",
                      ("suggestion", "accept"): "sugg:accept", ("suggestion", "reject"): "sugg:reject",
                      ("undo",): "edit:undo", ("section",): "section:add",
                      ("song", "next"): "song:next", ("song", "prev"): "song:prev", ("loop",): "loop:toggle"}
            action = simple.get(tuple(parts))
        if action:
            self._event.emit(action, press, t)

    # ------------------------------------------------------------------ actions (UI thread)
    def lane_id(self, n: int) -> str | None:
        lanes = self.s.project.lanes
        return lanes[n - 1].id if 1 <= n <= len(lanes) else None

    @Slot(str, bool, float)
    def _dispatch(self, action: str, pressed: bool, t: float) -> None:
        if getattr(self, "_fb_needed", False):         # the device's channel changed: relight pads
            self._fb_needed = False
            QTimer.singleShot(0, self.send_feedback)
        if action.startswith(("cue:", "temp:")):
            temp = action.startswith("temp:")
            if action.endswith(":active"):
                lane = self.s.active_lane_id
            else:
                lane = self.lane_id(int(action.rsplit(":", 1)[1]))
            if not lane:
                return
            if pressed:
                c = self.s.add_at_playhead(temp, lane, t)
                if c is not None and temp and self.cfg.hold_from_press:
                    self._pending[action] = (c.id, c.time)
                self._flash(action)
            elif temp and action in self._pending:
                cid, t0 = self._pending.pop(action)
                self.s.finish_temp(cid, t)             # standard length if short, snapped end
            return
        if not pressed:
            return
        if action == "lane:next":
            self.s.step_active_lane(1)
        elif action == "lane:prev":
            self.s.step_active_lane(-1)
        elif action in self.callbacks:
            self.callbacks[action]()

    # ------------------------------------------------------------------ feedback
    def _pad_velocity(self, action: str) -> int:
        if not action.startswith(("cue:lane:", "temp:lane:")):
            return 0
        lid = self.lane_id(int(action.rsplit(":", 1)[1]))
        lane = self.s.project.lane(lid) if lid else None
        if lane is None:
            return 0
        mode = self.feedback_mode()
        if mode == "onoff":
            return 127
        active = lid == self.s.active_lane_id
        dim = not active and action.startswith("temp:")
        if mode == "midifighter":
            return midifighter_velocity(lane.color, dim=dim)
        return color_velocity(lane.color, dim=dim)

    def feedback_messages(self) -> list:
        import mido
        out = []
        if self.feedback_mode() == "off":
            return out
        for m in self.cfg.mappings():
            if m.kind != "note":
                continue
            v = m.led if m.led >= 0 else self._pad_velocity(m.action)
            out.append(mido.Message("note_on", channel=self.feedback_channel(m), note=m.number,
                                    velocity=max(0, min(127, int(v)))))
        return out

    def test_lights(self, step: int = 8) -> list[tuple[int, int]]:
        """Light every mapped pad with a different LED value (0, 8, 16 …) so you can see which
        number gives which colour on your controller. Returns [(note, velocity)]."""
        import mido
        shown = []
        if self._out is None:
            return shown
        notes = sorted({m.number for m in self.cfg.mappings() if m.kind == "note"})
        ch = next((self.feedback_channel(m) for m in self.cfg.mappings() if m.kind == "note"), 0)
        for i, n in enumerate(notes):
            v = min(127, i * step)
            try:
                self._out.send(mido.Message("note_on", channel=ch, note=n, velocity=v))
            except Exception:
                break
            shown.append((n, v))
        return shown

    def send_feedback(self) -> None:
        if self._out is not None:
            try:
                for msg in self.feedback_messages():
                    self._out.send(msg)
            except Exception:
                pass
        if self._osc_client is not None:
            try:
                lanes = self.s.project.lanes
                self._osc_client.send_message("/cueforge/lanes", len(lanes))
                for i, lane in enumerate(lanes, 1):
                    self._osc_client.send_message(f"/cueforge/lane/{i}/name", lane.name)
                    self._osc_client.send_message(f"/cueforge/lane/{i}/color", lane.color)
                    self._osc_client.send_message(f"/cueforge/lane/{i}/active",
                                                  1 if lane.id == self.s.active_lane_id else 0)
                self._osc_client.send_message("/cueforge/song", self.s.project.song.name)
            except Exception:
                pass

    def _flash(self, action: str) -> None:
        if self._out is None or self.cfg.feedback == "off":
            return
        import mido
        for m in self.cfg.mappings():
            if m.action == action and m.kind == "note":
                try:
                    flash = 127 if self.feedback_mode() in ("midifighter", "onoff") else WHITE
                    self._out.send(mido.Message("note_on", channel=self.feedback_channel(m), note=m.number,
                                                velocity=flash))
                except Exception:
                    return
        QTimer.singleShot(120, self.send_feedback)

    def _send_tc(self) -> None:
        if self._osc_client is None or not self.s.engine.playing:
            return
        from ..core.timecode import seconds_to_tc
        p = self.s.project
        try:
            self._osc_client.send_message("/cueforge/tc", seconds_to_tc(self.s.engine.position(), p.frame_rate,
                                                                         p.tc_offset))
        except Exception:
            pass

    # ------------------------------------------------------------------
    def close(self) -> None:
        for port in (self._in, self._out):
            try:
                if port is not None:
                    port.close()
            except Exception:
                pass
        self._in = self._out = None
        if self._osc_server is not None:
            try:
                self._osc_server.shutdown()
                self._osc_server.server_close()
            except Exception:
                pass
        self._osc_server = None
        self._osc_client = None
