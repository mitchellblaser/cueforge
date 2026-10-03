# CueForge — Plan

Personal desktop tool for planning lighting hits/cues against audio, with AI
*suggestions* that the programmer must confirm. Target console: grandMA3.

## Principles
- AI never creates, moves or deletes a confirmed cue on its own.
- Suggestions are visually distinct (ghosted, dashed) and carry a confidence + reason.
- Manual programming must be fast with AI turned off.
- Frame-accurate timing (24, 25, 29.97 DF, 30 fps). Times stored as float seconds.

## Stack
Python 3.10+ · PySide6 (UI) · sounddevice + numpy (multitrack playback/mixer) ·
soundfile/ffmpeg (decode) · librosa (baseline analysis) · optional Beat This!,
Demucs (advanced AI) · PyInstaller (packaging).

## Features
1. Multitrack import with roles (Track, Stem, Click, Cue/Guide, Other), per-track offset.
2. Small mixer: fader, mute, solo, meter per track; master; generated click; cue blips.
3. Timeline: waveform lanes, beat grid, timecode ruler, cue lanes, suggestion lanes.
4. Editing: tap-to-mark per lane, drag, nudge (frame/beat), snap, multi-select, undo/redo,
   loop region, varispeed.
5. AI suggestions: beat grid (from click track or detection), hits (onsets, per stem),
   sections (structure), energy (builds/drops/silences). Accept/reject/batch, confidence
   filters, tuning from accept/reject history.
6. Export: grandMA3 timecode XML, CSV cue list, LTC WAV.

## Milestones
| # | Milestone | Status |
|---|---|---|
| 0 | Spike — analysis accuracy on test audio; MA3 format | Done (synthetic ground-truth tests; MA3 XML needs onPC verification) |
| 1 | Core — import, waveform, playback, mixer, click, markers, ruler, save/load, CSV | Done |
| 2 | AI v1 — grid, onsets, sections, review UI | Done |
| 3 | MA3 export — XML, Lua plugin, command list, LTC WAV | Done |
| 4 | AI v2 — per-stem hits (imported stems / Demucs), energy, batch accept, threshold learning | Done |
| 5 | Live music & beyond drums — tempo-following beats, meter, tap-along grid, drum fills → strobes, chord changes → colour, lead lines → chase steps, multi-genre accuracy corpus | Done (see docs/ACCURACY.md) |
| 6 | Setlist: multiple songs per project with own timecode / MA3 slot / cue range; fast-fill strobe detection | Done |
| 7 | Later — live LTC/MTC output, OSC to console | Not started |

## Known limitations / next steps
- grandMA3 timecode XML layout is undocumented: verify an import in grandMA3 onPC; fall back to the Lua plugin.
- Deep-learning backends (Beat This!, Demucs, All-In-One) are wired in and tested with stand-in
  models; real weights download on first use and were not exercised in CI.
- Analysis accuracy is verified on synthetic songs; tune thresholds on your own material
  (the app learns thresholds from your accept/reject decisions).
- Undo covers cues, lanes, suggestions and the beat grid; mixer moves are not undoable.
