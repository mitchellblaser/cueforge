"""Application entry point."""
from __future__ import annotations

import os
import sys


def self_test() -> int:
    """Headless check that decoding, analysis, mixing and export work (also in packaged builds)."""
    import tempfile

    import numpy as np
    import soundfile as sf

    from .analysis.pipeline import AnalysisOptions, run_analysis
    from .audio.engine import AudioEngine
    from .audio.loader import load_audio
    from .core import editing
    from .core.model import Project, Track
    from .export.ltc import decode_ltc, generate_ltc
    from .export.ma3 import build_ma3_xml

    sr = 44100
    y = np.zeros(sr * 12, np.float32)
    t = np.arange(int(sr * 0.05)) / sr
    for k in range(24):  # 120 BPM clicks
        i = int(k * 0.5 * sr)
        y[i:i + len(t)] += np.sin(2 * np.pi * 1000 * t) * np.exp(-t / 0.01)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        sf.write(path, y, sr)
        audio = load_audio(path)
        p = Project()
        tr = Track("test", path)
        p.tracks.append(tr)
        res = run_analysis(p, {tr.id: audio}, AnalysisOptions(use_deep_models=False, sections=False))
        bpm = res.grid.bpm() if res.grid else 0
        print(f"analysis ok: {bpm:.1f} BPM, {len(res.suggestions)} suggestions")
        eng = AudioEngine()
        eng.set_track(tr.id, audio.samples)
        print(f"mixer ok: peak {float(np.abs(eng.render(0.0, 4800)).max()):.2f}")
        editing.add_cue(p, p.lanes[0].id, 1.0, label="Test")
        print(f"ma3 xml ok: {len(build_ma3_xml(p))} bytes")
        frames = decode_ltc(generate_ltc(0, 10, p.frame_rate), 48000, p.frame_rate)
        print(f"ltc ok: {len(frames)} frames")
        from pythonosc.udp_client import SimpleUDPClient
        SimpleUDPClient("127.0.0.1", 9).send_message("/gma3/cmd", "Go+ Sequence 1")
        import mido  # noqa: F401
        from .control import ma3link  # noqa: F401
        print("osc/midi/ma3 link ok")
        try:
            import sounddevice as sd
            print(f"audio devices: {len(sd.query_devices())}")
        except Exception as exc:
            print(f"audio devices unavailable: {exc}")
    ok = abs(bpm - 120) < 2 and frames
    print("SELF-TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def _set_process_name() -> None:
    """Run from source, the OS calls the app "Python" (macOS menu bar / Dock, Windows
    taskbar). Name it CueForge instead. The packaged app is named by its bundle."""
    if sys.platform == "darwin":
        try:
            from Foundation import NSBundle  # pyobjc-framework-Cocoa
            bundle = NSBundle.mainBundle()
            for info in (bundle.localizedInfoDictionary(), bundle.infoDictionary()):
                if info is not None:
                    info["CFBundleName"] = "CueForge"
                    info["CFBundleDisplayName"] = "CueForge"
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CueForge.CueForge")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    from .addons import activate, handle_cli
    code = handle_cli(argv)          # --pip / --prefetch-models / --analysis-worker child modes
    if code is not None:
        return code
    activate()                       # AI models installed from the app
    if "--self-test" in argv:
        if sys.stdout is None:  # windowed build on Windows has no console: log to a file
            sys.stdout = sys.stderr = open("cueforge-self-test.log", "w", encoding="utf-8")
        return self_test()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    _set_process_name()
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName("CueForge")
    app.setApplicationDisplayName("CueForge")
    app.setOrganizationName("CueForge")
    from .ui.theme import apply_theme
    apply_theme(app)
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icon.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    from .ui.main_window import MainWindow
    win = MainWindow()
    win.restore_layout()
    win.show()
    from PySide6.QtCore import QTimer
    from .ui.ai_models_dialog import first_run_prompt
    QTimer.singleShot(1200, lambda: first_run_prompt(win.s.settings, win))
    args = [a for a in argv[1:] if not a.startswith("-")]
    if args:
        if args[0].endswith(".cueproj"):
            win.open_project(os.path.abspath(args[0]))
        else:
            win.s.import_audio([os.path.abspath(a) for a in args])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
