"""Git worktree isolation (M4-2).

Sub-agents should work in their own checked-out worktree on their own branch so
their file edits never collide with the main working copy.  ``WorktreeManager``
wraps the ``git worktree`` plumbing behind a small async API and an
``temporary()`` context manager for guaranteed cleanup.

Everything degrades loudly on non-repo dirs (``can_isolate()`` returns False)
and managed errors surface as :class:`WorktreeError`.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

_BRANCH_PREFIX = "ai_sub_"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""


@dataclass(frozen=True)
class Worktree:
    """A single checked-out worktree (path, branch)."""

    name: str
    branch: str
    path: Path


def _sanitize(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip("._") or "sub"
    return cleaned[:40]


class WorktreeManager:
    """Create/remove/list git worktrees rooted under a repository."""

    def __init__(self, repo_root: str | Path, base_dir: str | Path | None = None) -> None:
        self._root = Path(repo_root).expanduser().resolve()
        self._base = (
            Path(base_dir).expanduser().resolve()
            if base_dir is not None
            else self._root / "worktrees"
        )

    async def _git(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self._root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        text = (stdout + stderr).decode("utf-8", errors="replace").strip()
        return (proc.returncode or 0), text

    async def can_isolate(self) -> bool:
        """True when the root is inside a git work tree and git is callable."""
        try:
            code, _ = await self._git("rev-parse", "--is-inside-work-tree")
            return code == 0
        except (OSError, ValueError):
            return False

    async def list(self) -> list[str]:
        """Return the absolute paths of all linked worktrees."""
        code, text = await self._git("worktree", "list", "--porcelain")
        if code != 0:
            return []
        paths: list[str] = []
        for line in text.splitlines():
            if line.startswith("worktree "):
                paths.append(line[len("worktree "):])
        return paths

    async def create(self, name: str = "sub") -> Worktree:
        """Check out a new branch+worktree. Raises ``WorktreeError`` on failure."""
        safe = _sanitize(name)
        token = uuid.uuid4().hex[:8]
        branch = f"{_BRANCH_PREFIX}{safe}_{token}"
        path = self._base / f"{safe}_{token}"
        path.parent.mkdir(parents=True, exist_ok=True)
        code, text = await self._git("worktree", "add", "-b", branch, str(path))
        if code != 0:
            raise WorktreeError(f"git worktree add failed: {text}")
        return Worktree(name=safe, branch=branch, path=path)

    async def remove(self, path: str | Path) -> None:
        """Remove a worktree (forced). Raises ``WorktreeError`` on failure."""
        target = str(Path(path).expanduser().resolve())
        code, text = await self._git("worktree", "remove", "--force", target)
        if code != 0:
            raise WorktreeError(f"git worktree remove failed: {text}")
        leftover = Path(target)
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)

    @asynccontextmanager
    async def temporary(self, name: str = "sub") -> AsyncIterator[Worktree]:
        """Create a worktree for the block and remove it on exit (best-effort)."""
        wt = await self.create(name)
        try:
            yield wt
        finally:
            try:
                await self.remove(str(wt.path))
            except WorktreeError:
                pass  # best-effort cleanup; never mask the sub-agent's result


__all__ = ["WorktreeManager", "Worktree", "WorktreeError"]
