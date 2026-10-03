@echo off
REM Run CueForge from source on Windows (creates a virtual environment on first run).
cd /d %~dp0
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
start "" pythonw -m cueforge %*
