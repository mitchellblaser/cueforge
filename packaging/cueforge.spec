# PyInstaller spec for CueForge. Build with:  pyinstaller packaging/cueforge.spec
# (run from the repository root; see build_windows.bat / build_macos.sh)
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [(os.path.join(ROOT, "cueforge", "resources"), os.path.join("cueforge", "resources"))]
binaries = []
hiddenimports = collect_submodules("cueforge")

# librosa uses lazy_loader stubs (.pyi) and data files; soundfile/sounddevice ship native libs
for pkg in ("librosa", "soundfile", "sounddevice", "_soundfile_data", "_sounddevice_data", "soxr",
            "lazy_loader", "audioread", "mido", "rtmidi", "pythonosc"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass
hiddenimports += ["sklearn.utils._typedefs", "sklearn.neighbors._partition_nodes", "mido.backends.rtmidi"]

# pip inside the app installs the optional AI models (CueForge --pip …, AI ▸ AI models…)
d, b, h = collect_all("pip")
datas += d
binaries += b
hiddenimports += h

# PyTorch & co. are installed later, outside the bundle, and use standard-library modules
# CueForge itself never imports: ship the whole standard library.
import importlib.util
SKIP_STD = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "venv", "pydoc_data",
            "this", "antigravity", "xxsubtype", "xxlimited", "xxlimited_35", "_xxtestfuzz", "_testcapi"}
for name in sorted(getattr(sys, "stdlib_module_names", ())):
    if name in SKIP_STD or name.startswith("_"):
        continue
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        spec = None
    if spec is None:
        continue
    if spec.submodule_search_locations:
        hiddenimports += [m for m in collect_submodules(name, filter=lambda n: ".test" not in n)]
    else:
        hiddenimports.append(name)

# Optional deep-learning backends are large; include them only if requested
if os.environ.get("CUEFORGE_BUNDLE_AI") == "1":
    for pkg in ("torch", "torchaudio", "demucs", "beat_this", "julius", "einops", "rotary_embedding_torch"):
        try:
            d, b, h = collect_all(pkg)
            datas += d
            binaries += b
            hiddenimports += h
        except Exception:
            pass
    excludes = []
else:
    excludes = ["torch", "torchaudio", "demucs", "beat_this", "allin1", "tensorflow"]
excludes += ["tkinter", "matplotlib", "IPython", "PyQt5", "PyQt6", "pytest", "test", "idlelib", "lib2to3"]

icon = None
if sys.platform == "win32" and os.path.exists(os.path.join(SPECPATH, "icon.ico")):
    icon = os.path.join(SPECPATH, "icon.ico")
elif sys.platform == "darwin" and os.path.exists(os.path.join(SPECPATH, "icon.icns")):
    icon = os.path.join(SPECPATH, "icon.icns")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CueForge", console=False, icon=icon,
          upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CueForge", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(coll, name="CueForge.app", icon=icon, bundle_identifier="local.cueforge",
                 info_plist={"NSHighResolutionCapable": True,
                             "CFBundleShortVersionString": "1.0.0",
                             "CFBundleDocumentTypes": [{"CFBundleTypeName": "CueForge Project",
                                                        "CFBundleTypeExtensions": ["cueproj"],
                                                        "CFBundleTypeRole": "Editor"}]})
