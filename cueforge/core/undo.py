"""Snapshot-based undo/redo for cue, lane, suggestion and beat-grid edits."""
from __future__ import annotations

from typing import Any, Callable

from .model import Project


class UndoStack:
    def __init__(self, project: Project, limit: int = 200) -> None:
        self.project = project
        self.limit = limit
        self._undo: list[tuple[str, dict[str, Any]]] = []
        self._redo: list[tuple[str, dict[str, Any]]] = []
        # on_change(restored): restored=False after a new edit was recorded (the editor already
        # refreshed what it changed), True after undo / redo / clear replaced the project state
        self.on_change: Callable[[bool], None] | None = None
        self.clean_index = 0

    def push(self, label: str) -> None:
        """Call *before* mutating the project."""
        self._undo.append((label, self.project.edit_state()))
        if len(self._undo) > self.limit:
            self._undo.pop(0)
            self.clean_index -= 1
        self._redo.clear()
        self._changed(False)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1][0] if self._redo else ""

    def undo(self) -> bool:
        if not self._undo:
            return False
        label, state = self._undo.pop()
        self._focus(state)
        self._redo.append((label, self.project.edit_state()))
        self.project.restore_edit_state(state)
        self._changed()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        label, state = self._redo.pop()
        self._focus(state)
        self._undo.append((label, self.project.edit_state()))
        self.project.restore_edit_state(state)
        self._changed()
        return True

    def _focus(self, state: dict[str, Any]) -> None:
        """Snapshots belong to one song: switch to it before snapshotting the other side."""
        sid = state.get("song_id")
        if sid and hasattr(self.project, "select_song"):
            self.project.select_song(sid)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self.clean_index = 0
        self._changed()

    def mark_clean(self) -> None:
        self.clean_index = len(self._undo)

    def is_dirty(self) -> bool:
        return self.clean_index != len(self._undo)

    def _changed(self, restored: bool = True) -> None:
        if self.on_change:
            self.on_change(restored)
