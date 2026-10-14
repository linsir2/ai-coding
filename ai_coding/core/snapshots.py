"""Pre-turn workspace snapshots, pinned under refs/ai-coding/ (r17/T1, r18/F4).

``git stash create`` writes a commit of the current dirty tree *without*
touching ``refs/stash`` or the working tree; pinning it with ``update-ref``
keeps it out of gc's reach.  Restore is per file: files the snapshot knows are
checked out of it — the same split OpenCode's ``git.checkout``/``fs.remove``
pair implements (git.ts:596-623).

``git stash create`` does **not** capture untracked files (verified: neither it
nor ``git diff <ref>`` ever name them).  So restore also has to delete what the
turn created — but it is *told* what that is: ``created`` is the set of paths
the turn's own tools reported writing (``ToolArtifact``), carried on the tool
messages (r18/F4).  It used to be inferred from "untracked now and untracked
before", which could not tell a file the turn created from one the human typed
while the turn was running — and whose failure mode, when the baseline was
missing, was deleting every untracked file in the workspace.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ai_coding.core.gitcmd import git

#: ``<session_id>/<turn>`` (turn refs) or ``<session_id>/redo`` (the one redo
#: slot a session keeps).  Session ids are uuid hex; turns are decimal.
_KEY = re.compile(r"^[A-Za-z0-9_-]+/(?:[0-9]+|redo)$")


class SnapshotStore:
    """Capture pre-turn workspace state and restore it file by file."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        base_ref: str = "refs/ai-coding/undo",
        keep: int = 20,
        timeout: float = 60.0,
    ) -> None:
        self._root = Path(workspace).expanduser().resolve()
        self._base = base_ref
        self._keep = keep
        self._timeout = timeout

    async def _is_repo(self) -> bool:
        try:
            code, _ = await git(
                self._root, "rev-parse", "--is-inside-work-tree", timeout=self._timeout
            )
        except (OSError, TimeoutError):
            return False
        return code == 0

    def _ref(self, key: str) -> str:
        if not _KEY.match(key):
            raise ValueError(f"bad snapshot key {key!r}")
        return f"{self._base}/{key}"

    async def capture(self, key: str) -> str | None:
        """Snapshot the working tree; returns the ref, or None outside a repo."""
        if not await self._is_repo():
            return None
        code, out = await git(self._root, "stash", "create", timeout=self._timeout)
        sha = out.strip() if code == 0 else ""
        if not sha:  # a clean tree's pre-turn state *is* HEAD
            code, sha = await git(self._root, "rev-parse", "HEAD", timeout=self._timeout)
            if code != 0 or not sha:
                return None
        ref = self._ref(key)
        if (await git(self._root, "update-ref", ref, sha, timeout=self._timeout))[0] != 0:
            return None
        return ref

    async def exists(self, key: str) -> bool:
        try:
            code, _ = await git(
                self._root,
                "rev-parse",
                "--verify",
                "--quiet",
                self._ref(key),
                timeout=self._timeout,
            )
        except (OSError, TimeoutError):
            return False
        return code == 0

    async def restore(self, key: str, *, created: set[str] | None = None) -> list[str]:
        """Bring every path that differs from the snapshot back to it.

        ``created`` names the files the turn created; those are removed when the
        snapshot never knew them.  Anything untracked that the turn did not
        report writing is left alone -- it may be the human's own work.
        """
        ref = self._ref(key)
        code, out = await git(self._root, "diff", "--name-only", ref, timeout=self._timeout)
        if code != 0:
            return []
        paths = {line.strip() for line in out.splitlines() if line.strip()}
        restored: set[str] = set()
        for rel in sorted(paths):
            target = self._root / rel
            code, _ = await git(self._root, "cat-file", "-e", f"{ref}:{rel}", timeout=self._timeout)
            if code == 0:
                await git(self._root, "checkout", ref, "--", rel, timeout=self._timeout)
            elif rel in (created or set()):
                _remove(target)
            restored.add(rel)
        # Files the turn created never appear in ``git diff`` (git does not track
        # them), so they are cleaned from the set the tools reported.
        for rel in sorted((created or set()) - paths):
            _remove(self._root / rel)
            restored.add(rel)
        return sorted(restored)

    async def prune(self, session_id: str) -> None:
        """Keep only the newest ``keep`` refs of one session."""
        code, out = await git(
            self._root,
            "for-each-ref",
            "--format=%(refname)",
            f"{self._base}/{session_id}/",
            timeout=self._timeout,
        )
        if code != 0:
            return
        # Only numbered turn refs are a window to trim; a session's single
        # ``redo`` slot shares the namespace but is not one of them.
        turn_refs = [
            line
            for line in out.splitlines()
            if line.strip() and line.rsplit("/", 1)[-1].isdigit()
        ]
        turn_refs.sort(key=lambda ref: int(ref.rsplit("/", 1)[-1]))
        for ref in turn_refs[: max(0, len(turn_refs) - self._keep)]:
            await git(self._root, "update-ref", "-d", ref, timeout=self._timeout)


def _remove(target: Path) -> None:
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)


__all__ = ["SnapshotStore"]
