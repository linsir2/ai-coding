"""Bytes of the files a turn created, kept for ``/redo`` (P4).

``SnapshotStore`` restores tracked files from a pinned git commit, but
``git stash create`` never captures untracked ones -- so the only copy of a file
a turn *created* is the one undo reads just before removing it.  That copy used
to live in a loop-local dict and died with the process; a redo after a restart
then had nothing to write back.  It lives beside the workspace instead, under
the same ``.ai-coding`` tree the snapshots use.

Same split as the pending-approval marker: the session file holds what the
conversation *is*, and small independent payloads get their own place.
"""

from __future__ import annotations

import shutil
from pathlib import Path

__all__ = ["UndoFileStore"]


class UndoFileStore:
    """Session-scoped storage for the files an undo removed."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _resolve(self, session_id: str, rel: str) -> Path | None:
        """Resolve one workspace-relative path inside the store.

        The paths come from tool artifacts, so a ``..`` or an absolute path
        would otherwise be a write outside the store; refusing it keeps this
        a storage helper rather than a filesystem-writing hole.
        """
        base = self._dir(session_id)
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            return None
        return candidate

    def save(self, session_id: str, files: dict[str, bytes]) -> None:
        """Merge ``{workspace-relative path: bytes}`` into the store.

        Best effort: undo has already happened, and failing to keep a copy for
        a later redo must not turn that into an error the user sees.
        """
        for rel, data in files.items():
            target = self._resolve(session_id, rel)
            if target is None:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError:
                continue

    def load(self, session_id: str) -> dict[str, bytes]:
        """Every stored file, keyed by its workspace-relative path."""
        base = self._dir(session_id)
        if not base.is_dir():
            return {}
        out: dict[str, bytes] = {}
        for path in base.rglob("*"):
            if path.is_file():
                out[str(path.relative_to(base))] = path.read_bytes()
        return out

    def clear(self, session_id: str) -> None:
        """Drop everything one session staged."""
        shutil.rmtree(self._dir(session_id), ignore_errors=True)
