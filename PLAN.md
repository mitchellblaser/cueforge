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
0. Spike — analysis on test tracks; MA3 XML format.
1. Core — import, waveform, playback, mixer, click, markers, ruler, save/load, CSV.
2. AI v1 — grid, onsets, sections, review UI.
3. MA3 export — XML, LTC WAV.
4. AI v2 — per-stem hits, energy, batch rules, tuning.
5. Later — live LTC/MTC, OSC.
