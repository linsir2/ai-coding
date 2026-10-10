"""Workspace VCS facts for the TUI footer (M1): the current git branch.

Deliberately tiny and shell-based: the branch is a display string, and
``git rev-parse --abbrev-ref HEAD`` is the same one-liner every TUI uses.
Never raises — a missing git, a non-repository or a hung command all read
as "no branch" rather than as an error the UI would have to handle.
"""

from __future__ import annotations

import subprocess

__all__ = ["git_branch"]

_TIMEOUT_SECONDS = 3.0


def git_branch(workspace: str) -> str:
    """Branch name of ``workspace``, or ``""`` when there is none to show.

    ``git branch --show-current`` (not ``rev-parse``) because it works on an
    unborn branch — a fresh repository before its first commit, which is the
    normal state of a new workspace — and prints nothing when HEAD is
    detached, which is exactly the "no branch" answer wanted here.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
