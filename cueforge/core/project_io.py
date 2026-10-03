"""Loading and saving .cueproj files."""
from __future__ import annotations

import json
import os

from .model import Project

EXTENSION = ".cueproj"


def save_project(project: Project, path: str) -> None:
    path = os.path.abspath(path)
    base = os.path.dirname(path)
    data = project.to_dict()
    for t in [t for song in data["songs"] for t in song["tracks"]]:
        t["abs_path"] = os.path.abspath(t["path"])
        try:
            t["path"] = os.path.relpath(t["abs_path"], base)
        except ValueError:  # different drive on Windows
            t["path"] = t["abs_path"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    project.path = path
    project.name = os.path.splitext(os.path.basename(path))[0]


def load_project(path: str) -> Project:
    path = os.path.abspath(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != "cueforge-project":
        raise ValueError("Not a CueForge project file")
    base = os.path.dirname(path)
    all_tracks = list(data.get("tracks", [])) + [t for song in data.get("songs", []) for t in song.get("tracks", [])]
    for t in all_tracks:
        rel = os.path.normpath(os.path.join(base, t.get("path", "")))
        if os.path.exists(rel):
            t["path"] = rel
        elif t.get("abs_path") and os.path.exists(t["abs_path"]):
            t["path"] = t["abs_path"]
        else:
            t["path"] = rel  # missing; UI will offer to relink
    p = Project.from_dict(data)
    p.path = path
    p.name = os.path.splitext(os.path.basename(path))[0]
    return p


def analysis_cache_dir(project: Project) -> str:
    if project.path:
        d = os.path.splitext(project.path)[0] + "_cache"
    else:
        from .settings import user_data_dir
        d = os.path.join(user_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d
