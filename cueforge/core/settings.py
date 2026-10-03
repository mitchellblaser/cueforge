"""Per-user settings and learned tuning, stored as JSON in the user data dir."""
from __future__ import annotations

import json
import os
import sys
from typing import Any


def user_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    d = os.path.join(base, "CueForge")
    os.makedirs(d, exist_ok=True)
    return d


class UserSettings:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(user_data_dir(), "settings.json")
        self.data: dict[str, Any] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add_recent(self, path: str) -> None:
        recent = [p for p in self.get("recent", []) if p != path]
        recent.insert(0, path)
        self.set("recent", recent[:10])
