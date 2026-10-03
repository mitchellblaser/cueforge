#!/bin/bash
# Run CueForge from source on macOS (double-click in Finder). Creates a venv on first run.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && source .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt
else
  source .venv/bin/activate
fi
python -m cueforge "$@"
