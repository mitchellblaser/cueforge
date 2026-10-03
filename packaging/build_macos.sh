#!/bin/bash
# Build CueForge.app for macOS. Run from the repository root:
#   bash packaging/build_macos.sh
# Set CUEFORGE_BUNDLE_AI=1 to also bundle the optional deep-learning models (much larger).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
if [ "${CUEFORGE_BUNDLE_AI:-0}" = "1" ]; then pip install -r requirements-ai.txt; fi
python packaging/make_icon.py
python -m pytest -q
pyinstaller --noconfirm packaging/cueforge.spec
dist/CueForge.app/Contents/MacOS/CueForge --self-test
echo
echo "Done: dist/CueForge.app"
echo "First launch: right-click the app > Open (it is not code-signed)."
