"""Workspace path sandbox — all file-tool paths must resolve inside the workspace."""

from __future__ import annotations

from pathlib import Path


class SandboxViolation(Exception):
    """Raised when a path resolves outside the configured workspace."""


class Sandbox:
    """Resolve and validate paths against a workspace root.

    Paths that escape the workspace (via ``..``, absolute paths, symlinks, etc.)
    raise ``SandboxViolation``.
    """

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()

    def resolve(self, path: str | Path) -> Path:
        """Resolve ``path`` and verify it lies within the workspace."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        resolved = p.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise SandboxViolation(
                f"path '{path}' resolves outside workspace '{self.workspace}'"
            ) from exc
        return resolved


__all__ = ["Sandbox", "SandboxViolation"]
