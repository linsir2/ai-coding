"""File state tracker — remembers last-seen file snapshots for stale-read checks.

Used by ``write`` and ``edit`` tools to prevent blind writes and to detect
external modifications between a read and a write.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path


class FileState(enum.Enum):
    NEVER_READ = "never_read"
    CLEAN = "clean"
    STALE = "stale"


@dataclass(frozen=True)
class FileSnapshot:
    mtime: float
    size: int


class FileStateTracker:
    """Tracks the last-known state of files the model has read."""

    def __init__(self) -> None:
        self._snapshots: dict[Path, FileSnapshot] = {}

    def record(self, path: str | Path) -> None:
        """Record the current state of a file (call after a successful read)."""
        p = Path(path).resolve()
        try:
            st = p.stat()
            self._snapshots[p] = FileSnapshot(mtime=st.st_mtime, size=st.st_size)
        except OSError:
            # file vanished between read and record; just skip
            self._snapshots.pop(p, None)

    def check(self, path: str | Path) -> FileState:
        """Check whether the file is clean, stale, or has never been read."""
        p = Path(path).resolve()
        snap = self._snapshots.get(p)
        if snap is None:
            return FileState.NEVER_READ
        try:
            st = p.stat()
        except OSError:
            # file was deleted externally → treat as stale
            return FileState.STALE
        if st.st_mtime == snap.mtime and st.st_size == snap.size:
            return FileState.CLEAN
        return FileState.STALE

    def forget(self, path: str | Path) -> None:
        p = Path(path).resolve()
        self._snapshots.pop(p, None)


__all__ = ["FileStateTracker", "FileState", "FileSnapshot"]
