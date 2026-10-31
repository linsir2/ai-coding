"""Write tool — write/overwrite a file with stale-read protection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ai_coding.domain.message import ToolArtifact, ToolResult
from ai_coding.security.file_state_tracker import FileState, FileStateTracker
from ai_coding.security.sandbox import (
    Sandbox,
    SandboxViolation,
    open_inside,
    relative_to_root,
)
from ai_coding.tools.base import BaseTool, ToolContext

_DEFAULT_MAX_CHARS = 2_000_000  # 2M chars


class WriteTool(BaseTool):
    """Write content to a file, creating parent directories as needed.

    Enforces stale-read protection: overwriting an existing file requires that
    the model has previously read it and the file hasn't changed since.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        file_state_tracker: FileStateTracker,
        max_chars: int = _DEFAULT_MAX_CHARS,
        *,
        plan_root: Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker
        self._max_chars = max_chars
        self._plan_root = Path(plan_root) if plan_root is not None else None

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file, overwriting if it exists. "
            "Parent directories are created automatically. "
            "You MUST read the file first before overwriting it (stale-read protection)."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (relative to workspace).",
                },
                "content": {"type": "string", "description": "Content to write."},
            },
            "required": ["path", "content"],
        }

    def precheck(self, params: dict[str, Any]) -> ToolResult | None:
        path_str = str(params.get("path", ""))
        if not path_str:
            return None
        try:
            self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")
        return None

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        path_str = str(params.get("path", ""))
        content = str(params.get("content", ""))

        if len(content) > self._max_chars:
            msg = (
                f"content too large ({len(content)} chars > {self._max_chars} limit)"
            )
            return ToolResult(success=False, output=msg)

        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")

        if ctx is not None and ctx.mode == "plan":
            relative = self._plan_relative(path_str)
            if self._plan_root is None or relative is None:
                return ToolResult(
                    success=False,
                    output="计划模式下只能写入计划目录。",
                    error="plan_directory_required",
                )
            return self._write_plan_file(path_str, relative, content, target)

        # Stale-read check for existing files
        if target.exists():
            state = self._tracker.check(target)
            if state == FileState.NEVER_READ:
                msg = (
                    f"cannot write to '{path_str}': you must read the file first "
                    "(stale-read protection)"
                )
                return ToolResult(success=False, output=msg, error="must_read_first")
            if state == FileState.STALE:
                msg = (
                    f"cannot write to '{path_str}': file has been modified externally; "
                    "re-read it first"
                )
                return ToolResult(success=False, output=msg, error="stale_read")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, output=f"write error: {exc}")

        self._tracker.record(target)
        return ToolResult(
            success=True,
            output=f"wrote {len(content)} chars to {path_str}",
            # r18/F4: the path we actually wrote, normalised to the workspace.
            # Undo needs the list of files a turn created, and this is the only
            # place that knows it -- git cannot tell "the model wrote this" from
            # "the human did, while the turn was running".
            artifacts=(
                ToolArtifact(
                    path=target.relative_to(self._sandbox.workspace).as_posix(),
                    kind="file",
                    bytes=target.stat().st_size,
                ),
            ),
        )

    def _plan_relative(self, path_str: str) -> Path | None:
        if self._plan_root is None:
            return None
        candidate = Path(path_str).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self._sandbox.workspace) / candidate
        return relative_to_root(candidate, self._plan_root)

    def _write_plan_file(
        self,
        path_str: str,
        relative: Path,
        content: str,
        target: Path,
    ) -> ToolResult:
        # target.exists() is only the NEVER_READ fast path for a brand-new
        # file and may misreport under a symlink race; the authoritative
        # verdict is the fstat below on the verified fd.
        existed = target.exists()
        try:
            # readwrite (not write) so O_TRUNC does not destroy the pre-stat
            # the stale check below needs; truncation happens after the check
            # via ftruncate on the same fd.
            opened = open_inside(
                self._plan_root or Path(),
                relative,
                mode="readwrite",
                create=True,
            )
        except (SandboxViolation, OSError) as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")
        try:
            pre = os.fstat(opened.fd)
            if existed:
                state = self._tracker.check_stat(
                    opened.path, mtime=pre.st_mtime, size=pre.st_size
                )
                if state == FileState.NEVER_READ:
                    return ToolResult(
                        success=False,
                        output=(
                            f"cannot write to '{path_str}': you must read the file first "
                            "(stale-read protection)"
                        ),
                    )
                if state == FileState.STALE:
                    return ToolResult(
                        success=False,
                        output=(
                            f"cannot write to '{path_str}': file has been modified "
                            "externally; re-read it first"
                        ),
                    )
            data = content.encode("utf-8")
            os.ftruncate(opened.fd, 0)
            while data:
                written = os.write(opened.fd, data)
                data = data[written:]
            post = os.fstat(opened.fd)
            size = post.st_size
        except OSError as exc:
            return ToolResult(success=False, output=f"write error: {exc}")
        finally:
            os.close(opened.fd)

        self._tracker.record_stat(opened.path, mtime=post.st_mtime, size=post.st_size)
        return ToolResult(
            success=True,
            output=f"wrote {len(content)} chars to {path_str}",
            artifacts=(
                ToolArtifact(
                    path=opened.path.relative_to(self._sandbox.workspace).as_posix(),
                    kind="file",
                    bytes=size,
                ),
            ),
        )


__all__ = ["WriteTool"]
