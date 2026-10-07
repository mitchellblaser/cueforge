"""Optional AI models (Beat This!, Demucs): install, activate, remove, without pip by hand.

* Packaged app: packages go into a private folder in the user's data directory with the
  pip that ships inside CueForge (`CueForge --pip …`), and that folder is added to the
  import path at start-up. Only ready-made wheels are used, so no compiler or git is needed.
* From source: packages are installed into the Python environment CueForge runs in.

Model weights download into the same data directory (TORCH_HOME), so "Remove" really frees
the space. Everything here is plain functions; the dialog lives in ui/ai_models_dialog.py.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    module: str
    packages: tuple[str, ...]
    size: str
    what: str
    torch: bool = True        # needs PyTorch


TORCH = ("torch", "torchaudio")
COMPONENTS = (
    Component("beat_this", "Beat This!", "beat_this", ("beat_this>=1.0",), "≈ 80 MB",
              "Deep-learning beat and downbeat tracker. Much better grids on live recordings, "
              "rubato and tricky intros."),
    Component("demucs", "Demucs", "demucs", ("demucs>=4.0",), "≈ 300 MB",
              "Separates a stereo mix into drums / bass / vocals / other, so hits, fills and lead "
              "lines are found as if you had stems (slow on CPU; results are cached)."),
    Component("spoken_cues", "Spoken cue words", "sherpa_onnx", ("sherpa-onnx>=1.10",), "≈ 45 MB",
              "Recognises the section names on a spoken cue track (\"Verse\", \"Chorus\", \"Bridge\" …) "
              "so sections get their real names. Small, no PyTorch needed. Without it, cue-track "
              "sections are still found and grouped by sound.", torch=False),
)
TORCH_SIZE = "≈ 250 MB (macOS / Windows CPU) · ≈ 2.5 GB with NVIDIA GPU support"
GPU_INDEX = "https://download.pytorch.org/whl/cu124"


def data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "CueForge")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/CueForge")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "cueforge")


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def packages_dir() -> str:
    """Private package folder (packaged app). Per Python version: wheels are ABI specific.
    CUEFORGE_AI_DIR points somewhere else (e.g. an existing environment's site-packages)."""
    if os.environ.get("CUEFORGE_AI_DIR"):
        return os.environ["CUEFORGE_AI_DIR"]
    return os.path.join(data_dir(), "ai-packages", f"py{sys.version_info[0]}{sys.version_info[1]}")


def models_dir() -> str:
    return os.path.join(data_dir(), "ai-models")


def private_install() -> bool:
    """Install into the private folder (always for the packaged app; for a source checkout
    only when the environment can't be written to, or CUEFORGE_AI_PRIVATE=1)."""
    if frozen() or os.environ.get("CUEFORGE_AI_PRIVATE") == "1":
        return True
    import sysconfig
    target = sysconfig.get_paths()["purelib"]
    return not os.access(target, os.W_OK)


def activate() -> None:
    """Make installed AI packages importable and keep their model downloads in our folder."""
    d = packages_dir()
    if os.path.isdir(d) and d not in sys.path:
        sys.path.append(d)          # after the bundled packages, so ours (numpy…) win
        importlib.invalidate_caches()
    os.environ.setdefault("TORCH_HOME", models_dir())


def installed(c: Component) -> bool:
    try:
        ok = importlib.util.find_spec(c.module) is not None and \
            (not c.torch or importlib.util.find_spec("torch") is not None)
    except (ImportError, ValueError):
        return False
    if ok and c.key == "spoken_cues":
        from .analysis.cuetrack import spotter_available
        ok = spotter_available()
    return ok


def status() -> dict[str, bool]:
    importlib.invalidate_caches()
    return {c.key: installed(c) for c in COMPONENTS}


def any_installed() -> bool:
    return any(status().values())


def _self_command(*args: str) -> tuple[str, list[str], dict]:
    """Run CueForge's helper modes in a child process: the packaged exe takes them as flags,
    a source checkout runs `python -m cueforge.addons`."""
    if frozen():
        return sys.executable, list(args), {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pp = os.environ.get("PYTHONPATH", "")
    return sys.executable, ["-m", "cueforge.addons", *args], \
        {"PYTHONPATH": root + (os.pathsep + pp if pp else "")}


def pip_args(keys: list[str], gpu: bool = False) -> list[str]:
    chosen = [c for c in COMPONENTS if c.key in keys]
    pkgs = list(TORCH) if any(c.torch for c in chosen) else []
    for c in COMPONENTS:
        if c.key in keys:
            pkgs += list(c.packages)
    args = ["install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
            "--only-binary=:all:", "--prefer-binary"]
    if private_install():
        args += ["--target", packages_dir(), "--upgrade"]
    if gpu and sys.platform != "darwin" and any(c.torch for c in chosen):
        args += ["--extra-index-url", GPU_INDEX]
    return args + pkgs


def install_command(keys: list[str], log: str, gpu: bool = False) -> tuple[str, list[str], dict]:
    return _self_command("--pip", log, *pip_args(keys, gpu))


def prefetch_command(keys: list[str], log: str) -> tuple[str, list[str], dict]:
    return _self_command("--prefetch-models", log, *keys)


def uninstall(keys: list[str] | None = None) -> str:
    """Remove the AI packages (all of them, private folder) and downloaded models."""
    msgs = []
    if private_install():
        shutil.rmtree(os.path.join(data_dir(), "ai-packages"), ignore_errors=True)
        msgs.append("Removed the AI packages folder")
    else:
        import subprocess
        names = [c.module for c in COMPONENTS if keys is None or c.key in keys]
        if keys is None:
            names += list(TORCH)
        subprocess.call([sys.executable, "-m", "pip", "uninstall", "-y", *names])
        msgs.append("Uninstalled " + ", ".join(names))
    shutil.rmtree(models_dir(), ignore_errors=True)
    msgs.append("Removed downloaded model weights")
    return ". ".join(msgs)


# ---------------------------------------------------------------- child-process modes
def _redirect(log: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(log)) or ".", exist_ok=True)
    f = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def _skip_command_scripts() -> None:
    """Don't create the packages' command-line tools (torchrun.exe …). On Windows pip builds
    them from launcher templates that it can't read from inside the packaged app
    ("Unable to locate finder for 'pip._vendor.distlib'"), and CueForge never uses them."""
    try:
        from pip._internal.operations.install import wheel
        wheel.PipScriptMaker.make_multiple = lambda self, specifications, options=None: []
    except Exception as exc:          # a pip without this class: install as usual
        print(f"(could not switch off script creation: {exc})", flush=True)


def run_pip(log: str, args: list[str]) -> int:
    """`CueForge --pip <log> install …`: pip inside CueForge, output to <log>."""
    _redirect(log)
    print("$ pip " + " ".join(args), flush=True)
    if "--target" in args:
        os.makedirs(args[args.index("--target") + 1], exist_ok=True)
    try:
        from pip._internal.cli.main import main as pip_main
    except Exception as exc:
        print(f"pip is not available: {exc}", flush=True)
        return 2
    if frozen() or "--target" in args:
        _skip_command_scripts()
    code = int(pip_main(args) or 0)
    print(f"[pip finished with code {code}]", flush=True)
    return code


def prefetch(log: str, keys: list[str]) -> int:
    """Download model weights now (instead of during the first analysis)."""
    _redirect(log)
    activate()
    code = 0
    for key in keys:
        try:
            if key == "beat_this":
                print("Downloading Beat This! weights…", flush=True)
                from beat_this.inference import Audio2Beats
                Audio2Beats(checkpoint_path="final0", device="cpu", dbn=False)
            elif key == "demucs":
                print("Downloading Demucs (htdemucs) weights…", flush=True)
                from demucs.pretrained import get_model
                get_model("htdemucs")
            elif key == "spoken_cues":
                from .analysis.cuetrack import download_spotter
                if not download_spotter(lambda m: print(m, flush=True)):
                    raise RuntimeError("download incomplete")
            print(f"{key}: ready", flush=True)
        except Exception as exc:
            print(f"{key}: could not fetch weights now ({exc}); they download on first use", flush=True)
            code = 1
    print("[prefetch finished]", flush=True)
    return code


def check(log: str) -> int:
    """`CueForge --check-ai <log>`: can the installed models be imported and run here?"""
    _redirect(log)
    activate()
    print("packages dir:", packages_dir(), "exists:", os.path.isdir(packages_dir()), flush=True)
    print("status:", status(), flush=True)
    code = 0
    try:
        import torch
        x = torch.ones(4) * 2
        print("torch", torch.__version__, "ok", float(x.sum()), flush=True)
    except Exception as exc:
        print("torch FAILED:", repr(exc), flush=True)
        return 1
    for c in COMPONENTS:
        if not installed(c):
            continue
        try:
            if c.key == "spoken_cues":
                from .analysis.cuetrack import Spotter
                Spotter()
            elif c.key == "beat_this":
                from beat_this.inference import Audio2Beats  # noqa: F401
                from beat_this.model.beat_tracker import BeatThis
                BeatThis()
            elif c.key == "demucs":
                from demucs.apply import apply_model  # noqa: F401
                from demucs.htdemucs import HTDemucs
                HTDemucs(sources=["drums", "bass", "other", "vocals"])
            print(c.key, "ok", flush=True)
        except Exception as exc:
            import traceback
            print(c.key, "FAILED:", repr(exc), traceback.format_exc(), flush=True)
            code = 1
    print("[check finished]", flush=True)
    return code


def handle_cli(argv: list[str]) -> int | None:
    """Helper modes of the CueForge executable; None if argv is a normal start."""
    if len(argv) > 2 and argv[1] == "--check-ai":
        return check(argv[2])
    if len(argv) > 2 and argv[1] == "--pip":
        return run_pip(argv[2], argv[3:])
    if len(argv) > 2 and argv[1] == "--prefetch-models":
        return prefetch(argv[2], argv[3:])
    if len(argv) > 2 and argv[1] == "--analysis-worker":
        activate()
        from .analysis.worker import run
        return run(argv[2])
    return None


if __name__ == "__main__":
    code = handle_cli([sys.argv[0], *sys.argv[1:]])
    sys.exit(2 if code is None else code)
