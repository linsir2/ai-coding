"""One bounded git-call shape for the two users: worktrees and snapshots (r17/T1).

The command *is* ``git`` in a directory; what this module owns is the bounded
invocation every caller needs (``core/worktree.py`` and ``core/snapshots.py``),
so a stuck git can never freeze a turn (``docs/r2-analysis.md`` 1.4-B).
"""

from __future__ import annotations

from pathlib import Path

from ai_coding.infra.subprocess_runner import run_process_async

DEFAULT_GIT_TIMEOUT = 60.0


async def git(
    root: Path | str,
    *args: str,
    stdin_data: str | None = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> tuple[int, str]:
    """Run one git command in ``root``; returns ``(returncode, stdout+stderr)``.

    ``stdin_data`` feeds the child's stdin (``run_process_async`` already
    supports it), which ``hash-object --stdin`` needs for the untracked
    baseline.  Raises ``OSError``/``TimeoutError`` only when git cannot be
    started or overran; a git *failure* is the caller's to interpret (probing
    callers rely on ``rev-parse`` returning non-zero).
    """
    outcome = await run_process_async(
        ("git", *args), cwd=str(root), timeout=timeout, stdin_data=stdin_data
    )
    if outcome.error is not None:
        raise OSError(outcome.error)
    if outcome.timed_out:
        raise TimeoutError(f"git {args[0] if args else 'git'} timed out after {timeout:g}s")
    return outcome.returncode or 0, (outcome.stdout + outcome.stderr).strip()


__all__ = ["DEFAULT_GIT_TIMEOUT", "git"]
