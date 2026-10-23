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

import os
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class OpenedFile:
    """A file opened through a verified, symlink-free descriptor chain."""

    fd: int
    relative_path: str
    path: Path


def relative_to_root(path: str | Path, root: str | Path) -> Path | None:
    """The lexical relative path below ``root``, or ``None`` outside it.

    Lexical only: no filesystem access, no symlink resolution.  This is only
    the shape check that lets a caller choose the descriptor opener.
    ``open_inside`` remains the enforcement point.
    """
    candidate = Path(os.path.normpath(os.fspath(path)))
    base = Path(os.path.normpath(os.fspath(root)))
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return None
    return relative if relative.parts else None


def _dir_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return flags | getattr(os, "O_CLOEXEC", 0)


def _walk_to(start_fd: int, parts: Sequence[str], *, create: bool) -> int:
    """Walk ``parts`` below ``start_fd``, returning the final directory fd.

    Every component is opened with ``O_NOFOLLOW | O_DIRECTORY`` relative to
    the previous fd; missing components are created with ``mkdirat`` when
    ``create`` is true.  Intermediate fds are closed; the caller still owns
    ``start_fd`` and the returned fd.
    """
    dir_flags = _dir_flags()
    cur = start_fd
    try:
        for part in parts:
            if not part or part in (".", "..") or "/" in part or "\\" in part:
                raise SandboxViolation(f"path component '{part}' is not a safe directory")
            try:
                child = os.open(part, dir_flags, dir_fd=cur)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=cur)
                try:
                    child = os.open(part, dir_flags, dir_fd=cur)
                except OSError as exc:
                    raise SandboxViolation(
                        f"directory component '{part}' is not a safe directory: {exc}"
                    ) from exc
            except OSError as exc:
                raise SandboxViolation(
                    f"directory component '{part}' is not a safe directory: {exc}"
                ) from exc
            if cur != start_fd:
                os.close(cur)
            cur = child
        return cur
    except Exception:
        if cur != start_fd:
            try:
                os.close(cur)
            except OSError:
                pass
        raise


def _open_directory(path: Path, *, create: bool) -> int:
    """Open an absolute directory without following any symlink component."""
    if not path.is_absolute():
        raise SandboxViolation(f"directory root must be absolute: {path}")
    missing = [
        function.__name__
        for function in (os.open, os.mkdir)
        if function not in os.supports_dir_fd
    ]
    if missing or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SandboxViolation(
            "this platform cannot enforce descriptor-relative plan writes"
        )

    flags = _dir_flags()
    fd = os.open("/", flags)
    try:
        final = _walk_to(fd, path.parts[1:], create=create)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    if final != fd:
        # _walk_to keeps start_fd open; "/" is only the stepping stone.
        try:
            os.close(fd)
        except OSError:
            pass
    return final


def open_inside(
    root: str | Path,
    path: str | Path,
    *,
    mode: str,
    create: bool = False,
) -> OpenedFile:
    """Open ``path`` below ``root`` through a symlink-free fd chain.

    The root and every intermediate directory are opened component by component
    with ``O_NOFOLLOW | O_DIRECTORY`` relative to the previous descriptor, so a
    directory swapped after an earlier permission check is refused rather than
    followed.  The final component is opened with ``O_NOFOLLOW`` too.
    """
    flags_by_mode = {
        "read": os.O_RDONLY,
        "readwrite": os.O_RDWR,
        "write": os.O_WRONLY | os.O_TRUNC,
        "append": os.O_WRONLY | os.O_APPEND,
    }
    try:
        file_flags = flags_by_mode[mode]
    except KeyError as exc:
        raise ValueError(f"unknown open_inside mode: {mode!r}") from exc
    if create and mode == "read":
        raise ValueError("create=True requires a writable mode")

    root_path = Path(root).expanduser()
    if not root_path.is_absolute():
        raise SandboxViolation(f"plan root must be absolute: {root}")
    relative = posixpath.normpath(str(path).replace("\\", "/"))
    parts = [part for part in relative.split("/") if part and part != "."]
    if (
        not parts
        or relative.startswith("../")
        or "/../" in f"/{relative}/"
        or relative.startswith("/")
    ):
        raise SandboxViolation(f"path must stay below the plan root: {path}")

    root_fd = _open_directory(root_path, create=create)
    try:
        parent_fd = _walk_to(root_fd, parts[:-1], create=create)
    except Exception:
        try:
            os.close(root_fd)
        except OSError:
            pass
        raise
    try:
        final_flags = file_flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        if create:
            final_flags |= os.O_CREAT
        try:
            fd = os.open(parts[-1], final_flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise SandboxViolation(
                f"file component '{parts[-1]}' is not safe to open: {exc}"
            ) from exc
        return OpenedFile(
            fd=fd,
            relative_path="/".join(parts),
            path=root_path.joinpath(*parts),
        )
    finally:
        if parent_fd != root_fd:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        try:
            os.close(root_fd)
        except OSError:
            pass


__all__ = [
    "OpenedFile",
    "RootGrantor",
    "Sandbox",
    "SandboxViolation",
    "open_inside",
    "relative_to_root",
]
