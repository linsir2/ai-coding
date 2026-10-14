"""Pre-turn workspace snapshots, pinned under refs/ai-coding/ (r17/T1).

``git stash create`` writes a commit of the current dirty tree *without*
touching ``refs/stash`` or the working tree; pinning it with ``update-ref``
keeps it out of gc's reach.  Restore is per file: files the snapshot knows are
checked out of it, files it never knew are removed — the same split OpenCode's
``git.checkout``/``fs.remove`` pair implements (git.ts:596-623).

``git stash create`` does **not** capture untracked files (verified: neither it
nor ``git diff <ref>`` ever name them), so a naive restore could not tell a file
the turn created from one the user already had.  The baseline is therefore
recorded explicitly: at capture the untracked list is written as a blob and
pinned under ``refs/ai-coding/untracked/<key>`` (a ref may point at a blob);
restore removes only untracked files that baseline does not name.
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
        baseline_ref: str = "refs/ai-coding/untracked",
        keep: int = 20,
        timeout: float = 60.0,
    ) -> None:
        self._root = Path(workspace).expanduser().resolve()
        self._base = base_ref
        self._baseline = baseline_ref
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
        # The untracked baseline: a blob listing what was already untracked.
        _, listed = await git(
            self._root, "ls-files", "--others", "--exclude-standard", timeout=self._timeout
        )
        code, blob = await git(
            self._root,
            "hash-object",
            "-w",
            "--stdin",
            stdin_data=listed,
            timeout=self._timeout,
        )
        if code == 0 and blob:
            await git(
                self._root,
                "update-ref",
                f"{self._baseline}/{key}",
                blob,
                timeout=self._timeout,
            )
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

    async def _baseline_paths(self, key: str) -> set[str]:
        code, out = await git(
            self._root, "cat-file", "blob", f"{self._baseline}/{key}", timeout=self._timeout
        )
        return set(out.splitlines()) if code == 0 else set()

    async def restore(self, key: str) -> list[str]:
        """Bring every path that differs from the snapshot back to it."""
        ref = self._ref(key)
        baseline = await self._baseline_paths(key)
        code, out = await git(self._root, "diff", "--name-only", ref, timeout=self._timeout)
        if code != 0:
            return []
        paths = {line.strip() for line in out.splitlines() if line.strip()}
        for rel in sorted(paths):
            target = self._root / rel
            code, _ = await git(self._root, "cat-file", "-e", f"{ref}:{rel}", timeout=self._timeout)
            if code == 0:
                await git(self._root, "checkout", ref, "--", rel, timeout=self._timeout)
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        # Files the turn created: untracked now, absent from the baseline.
        _, others = await git(
            self._root, "ls-files", "--others", "--exclude-standard", timeout=self._timeout
        )
        removed: set[str] = set()
        for rel in (line.strip() for line in others.splitlines() if line.strip()):
            if rel in baseline:
                continue
            target = self._root / rel
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
            removed.add(rel)
        return sorted(paths | removed)

    async def prune(self, session_id: str) -> None:
        """Keep only the newest ``keep`` refs of one session (both namespaces)."""
        for base in (self._base, self._baseline):
            code, out = await git(
                self._root,
                "for-each-ref",
                "--format=%(refname)",
                f"{base}/{session_id}/",
                timeout=self._timeout,
            )
            if code != 0:
                continue
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


__all__ = ["SnapshotStore"]
