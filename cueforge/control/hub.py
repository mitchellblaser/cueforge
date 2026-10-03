"""MIDI + OSC control: hardware taps, transport and review, with colour feedback.

Input arrives on background threads; each hit is timestamped with the heard playback
position right away, then handed to the UI thread through a queued Qt signal.
"""
from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .actions import WHITE, ControlSettings, MidiMapping, color_velocity

MIN_HOLD = 0.05


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
        if self.cfg.midi_in:
            try:
                self._in = mido.open_input(self.cfg.midi_in, callback=self._on_midi)
                self.status.emit(f"MIDI in: {self.cfg.midi_in}")
            except Exception as exc:
                self.status.emit(f"MIDI in failed: {exc}")
        if self.cfg.midi_out:
            try:
                self._out = mido.open_output(self.cfg.midi_out)
            except Exception as exc:
                self.status.emit(f"MIDI out failed: {exc}")

    def _on_midi(self, msg) -> None:            # rtmidi thread
        self.handle_midi(msg, self.s.engine.position())

    def handle_midi(self, msg, t: float) -> None:
        """Map one MIDI message (thread-safe; also used by tests)."""
        kind = {"note_on": "note", "note_off": "note", "control_change": "cc"}.get(msg.type)
        if kind is None:
            return
        number = msg.note if kind == "note" else msg.control
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
            if m.key() == (kind, msg.channel, number):
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
                c = self.s.project.cue(cid)
                if c is not None:
                    c.duration = round(max(MIN_HOLD, t - t0), 3)   # part of the same undo step
                    self.s.cues_changed.emit()
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
        if self.cfg.feedback == "onoff":
            return 127
        active = lid == self.s.active_lane_id
        return color_velocity(lane.color, dim=not active and action.startswith("temp:"))

    def feedback_messages(self) -> list:
        import mido
        out = []
        if self.cfg.feedback == "off":
            return out
        for m in self.cfg.mappings():
            if m.kind != "note":
                continue
            v = self._pad_velocity(m.action)
            out.append(mido.Message("note_on", channel=m.channel, note=m.number, velocity=v))
        return out

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
                    self._out.send(mido.Message("note_on", channel=m.channel, note=m.number, velocity=WHITE))
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
