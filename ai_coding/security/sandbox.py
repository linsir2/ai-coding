"""Workspace path sandbox — file-tool paths resolve inside the workspace or a root.

Two kinds of root, and the difference is who decided:

* the **workspace** — where the session was started; always allowed, no question;
* an **extra root** — somewhere else the operator pointed this workspace, either
  in config or by answering "always" to an approval prompt.

Both existed in spirit before and neither was reachable.  ``Sandbox`` refused
everything outside the workspace, and because ``read`` never reached the
permission gate, the only way out was ``bash`` — which the gate answers with a
question every single time (``bash`` is never grantable by policy).  Measured in
the hotel session and recorded in the audit: the boundary "形同虚设, 只制造摩擦力",
a speed bump with an exit sign pointing at the unguarded door.

So a refusal became a question.  Same fail-closed default: with no roots
configured and no approval answered, an outside path is still refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SandboxViolation(Exception):
    """Raised when a path resolves outside every root this sandbox knows."""


class RootGrantor(Protocol):
    """What the sandbox asks before refusing: "may this path, right now?".

    Implemented by :class:`~ai_coding.security.permission_gate.PermissionGate`.
    The sandbox depends on the *question*, not on the gate, so it stays
    constructible without one — a bare REPL and a sub-agent both do, and neither
    gains the ability to read outside its workspace.
    """

    def path_allowed(self, path: str | Path) -> bool: ...


class Sandbox:
    """Resolve and validate paths against a workspace root plus extra roots.

    Paths that escape every root (via ``..``, absolute paths, symlinks, etc.)
    raise :class:`SandboxViolation` — unless ``grantor`` says the operator has
    already authorized this one, which is how an "always" reaches the tool.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        extra_roots: tuple[str | Path, ...] | list[str | Path] = (),
        grantor: RootGrantor | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.extra_roots = tuple(Path(root).expanduser().resolve() for root in extra_roots)
        self._grantor = grantor

    @property
    def roots(self) -> tuple[Path, ...]:
        """Every root that resolves without a question, workspace first."""
        return (self.workspace, *self.extra_roots)

    def _contained_in(self, resolved: Path) -> Path | None:
        for root in self.roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return root
        return None

    def resolve(self, path: str | Path) -> Path:
        """Resolve ``path`` and verify it lies within a known root.

        Raises :class:`SandboxViolation` when it does not, unless the grantor
        has already authorized it.  The check is containment after full
        resolution, so a symlink pointing out of the workspace is refused the
        same way a literal ``..`` is.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if self._contained_in(resolved) is not None:
            return resolved
        # The operator may already have said yes to this directory; asking the
        # gate is what makes an "always" mean anything to the file tools.
        if self._grantor is not None and self._grantor.path_allowed(resolved):
            return resolved
        raise SandboxViolation(
            f"path '{path}' resolves outside workspace '{self.workspace}'"
            " — this needs your approval before a file tool can read it;"
            " grant the directory once, or add it to tools.extra_workspace_roots"
        )


__all__ = ["RootGrantor", "Sandbox", "SandboxViolation"]
