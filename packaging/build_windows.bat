@echo off
REM Build CueForge for Windows. Run from the repository root in a terminal:
REM   packaging\build_windows.bat
REM Set CUEFORGE_BUNDLE_AI=1 to also bundle the optional deep-learning models (much larger).
setlocal
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip
pip install -r requirements-dev.txt || goto :error
if "%CUEFORGE_BUNDLE_AI%"=="1" pip install -r requirements-ai.txt
python packaging\make_icon.py
python -m pytest -q || goto :error
pyinstaller --noconfirm packaging\cueforge.spec || goto :error
start /wait "" dist\CueForge\CueForge.exe --self-test
type cueforge-self-test.log
echo.
echo Done. The app is in dist\CueForge\CueForge.exe
exit /b 0
:error
echo Build failed.
exit /b 1
