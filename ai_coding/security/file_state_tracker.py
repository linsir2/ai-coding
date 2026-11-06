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
    """Tracks the last-known state of files *a session* has read.

    The key is ``(scope, path)``.  One registry is shared by every session in a
    process (``server/world.py`` builds it once), so a path-only key let
    session B write a file that only session A had read: the "read it first"
    rule is about what *this* conversation has seen.  ``scope`` is the session
    id; an empty scope keeps the old global behaviour for callers that have no
    session (tests, one-shot tools).
    """

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, Path], FileSnapshot] = {}

    def record_stat(
        self, path: str | Path, *, mtime: float, size: int, scope: str = ""
    ) -> None:
        """Remember an ``os.fstat`` result without touching the filesystem.

        The key is the fd-verified path itself (``opened.path`` for plan
        writes) -- deliberately not resolved, so a symlink swap between
        resolve and record cannot move the snapshot to a different key.
        Non-plan callers keep using :meth:`record`/:meth:`check`, which
        resolve for compatibility.
        """
        p = Path(path)
        self._snapshots[(scope, p)] = FileSnapshot(mtime=mtime, size=size)

    def check_stat(
        self, path: str | Path, *, mtime: float, size: int, scope: str = ""
    ) -> FileState:
        """Compare an ``os.fstat`` result against the remembered snapshot.

        Same key rule as :meth:`record_stat`: the fd-verified path, not the
        resolved one.
        """
        p = Path(path)
        snap = self._snapshots.get((scope, p))
        if snap is None:
            return FileState.NEVER_READ
        if snap.mtime == mtime and snap.size == size:
            return FileState.CLEAN
        return FileState.STALE

    def record(self, path: str | Path, scope: str = "") -> None:
        """Record the current state of a file (call after a successful read)."""
        p = Path(path).resolve()
        try:
            st = p.stat()
        except OSError:
            # file vanished between read and record; just skip
            self._snapshots.pop((scope, p), None)
            return
        self.record_stat(p, mtime=st.st_mtime, size=st.st_size, scope=scope)

    def check(self, path: str | Path, scope: str = "") -> FileState:
        """Check whether the file is clean, stale, or has never been read."""
        p = Path(path).resolve()
        snap = self._snapshots.get((scope, p))
        if snap is None:
            return FileState.NEVER_READ
        try:
            st = p.stat()
        except OSError:
            # file was deleted externally → treat as stale
            return FileState.STALE
        return self.check_stat(p, mtime=st.st_mtime, size=st.st_size, scope=scope)

    def forget(self, path: str | Path, scope: str = "") -> None:
        p = Path(path).resolve()
        self._snapshots.pop((scope, p), None)


__all__ = ["FileStateTracker", "FileState", "FileSnapshot"]
