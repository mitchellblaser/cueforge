# CueForge

A desktop tool for lighting programmers. Import a song (and its stems, click and guide
tracks), plan hits and cue changes against timecode, and export the result to
**grandMA3**.

CueForge can also **analyse the audio and suggest** hits, section changes, drops and the
beat grid. These are only suggestions. They show as ghosted markers until you accept them,
and the analysis never creates, moves or deletes a confirmed cue.

![lanes, waveform and suggestions](docs/screenshot.png)

---

## Install and run

### Run from source (Windows / macOS)

You need Python 3.10–3.12 ([python.org](https://www.python.org/downloads/)).

- **Windows:** double-click `run_cueforge.bat`
- **macOS:** double-click `run_cueforge.command` (first time: right-click › Open)

The first run creates a `.venv` folder and installs the dependencies, which takes a few
minutes. Or do it by hand:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate        # macOS
pip install -r requirements.txt
python -m cueforge               # optionally: python -m cueforge song.wav click.wav
```

### Build a standalone app

- **Windows:** `packaging\build_windows.bat` → `dist\CueForge\CueForge.exe`
- **macOS:** `bash packaging/build_macos.sh` → `dist/CueForge.app`
- **GitHub:** push the repo and run the *Build* workflow (Actions tab, or push a `v*` tag).
  It produces zipped Windows and macOS builds as downloadable artifacts.

Each build runs the test suite and then `CueForge --self-test` inside the packaged app.
The macOS app isn't code-signed, so open it the first time with right-click › Open.

### Optional: deep-learning models

The built-in analysis (librosa) needs no extra downloads. For stronger beat tracking and
stem separation:

```bash
pip install -r requirements-ai.txt     # PyTorch, Demucs, Beat This!
```

- **Beat This!** is used automatically for beat and downbeat tracking when installed. Its
  weights download on first use.
- **Demucs** separates the mix into drums, vocals and other stems, so hits can be detected
  per stem. Enable it in the *Analyse* dialog. It's slow on CPU, and results are cached
  next to the project.
- **All-In-One** (`pip install allin1`, optional) labels sections (verse, chorus, …) when
  installed.

If a model fails to load (for example, no internet on first use), CueForge falls back to
the built-in analysis.

---

## Workflow

1. **Import audio.** Use *File › Import audio* or drag files onto the window. Each file
   gets a mixer strip and a **role**, guessed from its filename:
   | Role | Used for |
   |---|---|
   | **Track** | the main mix; analysed |
   | **Stem** | drums, vocals, etc.; analysed per stem (a drum stem gives kick/snare hits) |
   | **Click** | builds the beat grid directly, sample-accurate |
   | **Cue/Guide** | spoken cues and count-ins; played, never analysed |
   | **Other** | played, never analysed |

   Set a per-track **offset** (⋯ button on the strip) if files don't start together.

2. **Mix.** Each strip has a fader (double-click resets to 0 dB), meter, **M**ute and
   **S**olo. There is also a generated **Click** (from the beat grid, with an accent on bar 1),
   **Cue blips** (a tick on every confirmed cue, so you can hear whether hits land) and **Master**.

3. **Analyse** (✦ Analyse, Ctrl+R). Suggestions appear in the lanes as dashed, ghosted
   markers, more opaque when confidence is higher:
   - ◇ **Hits**: kick, snare and accent onsets. They go to the *Hits* lane by default.
   - □ **Sections**: harmony, timbre or loudness changes, snapped to bar lines and labelled A/B/C;
     a repeated letter means a repeated section.
   - △ **Energy**: drops, breakdowns, builds, blackouts and returns.
   - **Beat grid**: shown dashed and orange until you click *Accept grid*.

4. **Review**
   - **Tab / Shift+Tab** jumps between suggestions; **A** accepts, **X** rejects. Either one
     moves on to the next suggestion.
   - **P** plays from 2 s before the selected suggestion or cue.
   - The *AI Suggestions* tab has a **confidence threshold** per type, the target lane per
     type, and **Accept all** / **Reject all**.
   - Accepted and rejected decisions persist. Re-analysing never brings back something you
     rejected and never duplicates a cue.
   - Over time CueForge learns thresholds from your decisions (*Apply learned
     thresholds*). It only offers them; it never applies them by itself.

5. **Program manually**
   - Press a lane's **tap key** (1, 2, 3, …) during playback to drop a cue at the playhead.
   - Double-click a lane to add a cue. Drag cues to move them (they snap to beats when
     **Snap** is on; hold Alt to stop snapping), or drag them into another lane.
   - **←/→** nudges by a frame, **Shift+←/→** by a beat. **G** snaps to the grid.
   - Double-click a cue to edit its label, MA3 cue number, fade, notes and exact timecode.
   - Loop a region with **Shift-drag in the ruler**, or with **I / O**, then **L**. Slow
     playback with **[** (0.75×, 0.5×).

6. **Export** (*File › Export*)
   - **grandMA3**: each lane becomes a timecode track targeting the lane's **MA3
     sequence**, and each cue becomes a *Goto* event. Cue numbers you leave blank are filled
     in automatically. Three formats:
     - **Timecode XML**: import into the Timecode pool.
     - **Lua plugin**: creates any missing (empty, labelled) cues and builds the timecode
       show on the console.
     - **Command list**: `Store`/`Label` lines that create the cues.
   - **CSV** cue list, for paperwork.
   - **LTC WAV**: SMPTE timecode audio, optionally stereo with the song mix on L and LTC on R.
     Set *Project settings › Song starts at timecode* (e.g. `01:00:00:00`) to leave room for
     pre-roll.

   Only confirmed cues are exported. Pending suggestions never are.

> **grandMA3 note:** MA doesn't publish the timecode XML schema. CueForge's XML follows
> the layout grandMA3 itself exports (v1.9–2.x). Times are stored in MA's internal units
> (1/16 777 216 s), and *Seconds* is also offered. **Test the import in grandMA3 onPC
> before a show.** If your version rejects it, use the **Lua plugin** export, which builds
> the show through the console's own object API.

---

## Keyboard shortcuts (F1 in the app)

| Key | Action |
|---|---|
| Space | Play / pause |
| Home / Enter, End | Go to start / end |
| 1 … 9 | Tap a cue into that lane (configurable per lane) |
| ← / → | Nudge selection 1 frame (no selection: playhead 1 beat) |
| Shift + ← / → | Nudge selection 1 beat (no selection: playhead 1 bar) |
| Tab / Shift+Tab | Next / previous suggestion |
| A / X | Accept / reject selected suggestions |
| P | Preview: play from 2 s before the selection |
| Delete | Delete selection |
| S / G | Snap on/off / snap selection to grid |
| I / O / L | Loop in / out / on-off |
| [ / ] | Playback speed |
| C / B | Click / cue blips on-off |
| F / Z | Follow playhead / zoom to fit |
| Ctrl + wheel, + / − | Zoom |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo |

---

## Project files

`.cueproj` is JSON. Audio paths are stored relative to the project file, so you can move a
folder containing both. Missing audio can be relinked from the mixer strip's ⋯ menu.
Projects with unsaved changes are autosaved every two minutes to `<project>.cueproj.autosave`.

## Development

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

```
cueforge/
  core/       model, timecode (incl. 29.97 DF), editing ops, undo, project I/O
  audio/      decoding/resampling/peaks, real-time multitrack engine (mixer, click, blips, varispeed, loop)
  analysis/   beat grid (click track, Beat This!, librosa), hits, sections, energy, Demucs, threshold learning
  export/     grandMA3 (XML, Lua, commands), CSV, LTC encoder/decoder
  ui/         Qt main window, timeline, mixer, panels, dialogs
tests/        unit, analysis-accuracy (synthetic song with known answers), UI, optional backends
packaging/    PyInstaller spec, build scripts, icon
```
