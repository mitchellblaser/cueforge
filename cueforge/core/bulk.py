"""Bulk import: turn a folder of songs into (song name, audio files) groups."""
from __future__ import annotations

import os

from ..audio.loader import AUDIO_EXTENSIONS


def _audio_in(folder: str) -> list[str]:
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names
            if n.lower().endswith(AUDIO_EXTENSIONS) and not n.startswith(".")
            and os.path.isfile(os.path.join(folder, n))]


def _audio_below(folder: str) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out += [os.path.join(dirpath, f) for f in sorted(filenames, key=str.lower)
                if f.lower().endswith(AUDIO_EXTENSIONS) and not f.startswith(".")]
    return out


PART_FOLDERS = ("stem", "stems", "click", "clicks", "multitrack", "multitracks", "tracks", "audio", "guide",
                "guides", "cues", "cue", "mix", "mixes", "bounce", "bounces", "drums", "vocals", "inst", "parts")


def _subdirs(folder: str) -> list[str]:
    try:
        return sorted((c for c in os.listdir(folder) if os.path.isdir(os.path.join(folder, c))
                       and not c.startswith(".")), key=str.lower)
    except OSError:
        return []


def song_groups_from_folder(root: str) -> list[tuple[str, list[str]]]:
    """Each sub-folder with audio is one song (its files: mix, stems, click — including files
    in deeper folders such as "Stems/"); each audio file loose in `root` is a song of its
    own. A folder whose sub-folders are themselves songs (Setlist/Song/Stems vs.
    Album/Song/…) is handled by treating a sub-folder with no audio of its own but several
    song-like children as a container."""
    groups: list[tuple[str, list[str]]] = []
    for path in _audio_in(root):
        groups.append((os.path.splitext(os.path.basename(path))[0], [path]))
    for d in _subdirs(root):
        full = os.path.join(root, d)
        own = _audio_in(full)
        children = _subdirs(full)
        parts = any(c.lower() in PART_FOLDERS or any(w in c.lower() for w in ("stem", "click", "multitrack"))
                    for c in children)
        if not own and len(children) > 1 and not parts and all(_audio_in(os.path.join(full, c)) for c in children):
            groups += song_groups_from_folder(full)          # a folder of song folders
            continue
        files = _audio_below(full)
        if files:
            groups.append((d, files))
    return groups


def song_groups_from_files(paths: list[str]) -> list[tuple[str, list[str]]]:
    """One song per audio file, named after the file."""
    return [(os.path.splitext(os.path.basename(p))[0], [p]) for p in sorted(paths, key=lambda x: os.path.basename(x).lower())]
