"""Edit tool — precise string replacement with stale-read protection."""

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


class EditTool(BaseTool):
    """Replace a string in a file. ``old_string`` must match exactly once."""

    def __init__(
        self,
        sandbox: Sandbox,
        file_state_tracker: FileStateTracker,
        *,
        plan_root: Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker
        self._plan_root = Path(plan_root) if plan_root is not None else None

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Replace an exact string in a file. The old_string must appear exactly once "
            "unless replace_all is True. You MUST read the file first (stale-read protection)."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "old_string": {"type": "string", "description": "The exact text to replace."},
                "new_string": {"type": "string", "description": "The replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false).",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
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
        old = str(params.get("old_string", ""))
        new = str(params.get("new_string", ""))
        replace_all = bool(params.get("replace_all", False))

        if not old:
            return ToolResult(success=False, output="old_string must not be empty")

        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")

        if not target.is_file():
            return ToolResult(success=False, output=f"file not found: {path_str}")

        if ctx is not None and ctx.mode == "plan":
            relative = self._plan_relative(path_str)
            if self._plan_root is None or relative is None:
                return ToolResult(
                    success=False,
                    output="计划模式下只能编辑计划目录。",
                    error="plan_directory_required",
                )
            return self._edit_plan_file(
                path_str,
                relative,
                target,
                old,
                new,
                replace_all,
            )

        # Stale-read check
        state = self._tracker.check(target)
        if state == FileState.NEVER_READ:
            msg = (
                f"cannot edit '{path_str}': you must read the file first "
                "(stale-read protection)"
            )
            return ToolResult(success=False, output=msg)
        if state == FileState.STALE:
            msg = (
                f"cannot edit '{path_str}': file has been modified externally; "
                "re-read it first"
            )
            return ToolResult(success=False, output=msg)

        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, output=f"read error: {exc}")

        count = text.count(old)
        if count == 0:
            return ToolResult(success=False, output=f"old_string not found in {path_str}")
        if not replace_all and count > 1:
            msg = (
                f"old_string appears {count} times in {path_str}; "
                "it must be unique or set replace_all=true"
            )
            return ToolResult(success=False, output=msg)

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)

        try:
            target.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, output=f"write error: {exc}")

        self._tracker.record(target)
        replaced = count if replace_all else 1
        return ToolResult(
            success=True,
            output=f"replaced {replaced} occurrence(s) in {path_str}",
            artifacts=(  # r18/F4: see write_tool -- undo replays this list
                ToolArtifact(path=target.relative_to(self._sandbox.workspace).as_posix()),
            ),
        )

    def _plan_relative(self, path_str: str) -> Path | None:
        if self._plan_root is None:
            return None
        candidate = Path(path_str).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self._sandbox.workspace) / candidate
        return relative_to_root(candidate, self._plan_root)

    def _edit_plan_file(
        self,
        path_str: str,
        relative: Path,
        target: Path,
        old: str,
        new: str,
        replace_all: bool,
    ) -> ToolResult:
        try:
            opened = open_inside(self._plan_root or Path(), relative, mode="readwrite")
        except (SandboxViolation, OSError) as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")
        try:
            # The stale defense is the fstat below on the verified fd, not a
            # path re-stat; target.is_file() above is only the not-found fast path.
            pre = os.fstat(opened.fd)
            state = self._tracker.check_stat(
                target, mtime=pre.st_mtime, size=pre.st_size
            )
            if state == FileState.NEVER_READ:
                return ToolResult(
                    success=False,
                    output=(
                        f"cannot edit '{path_str}': you must read the file first "
                        "(stale-read protection)"
                    ),
                )
            if state == FileState.STALE:
                return ToolResult(
                    success=False,
                    output=(
                        f"cannot edit '{path_str}': file has been modified externally; "
                        "re-read it first"
                    ),
                )
            os.lseek(opened.fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(opened.fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
            count = text.count(old)
            if count == 0:
                return ToolResult(
                    success=False,
                    output=f"old_string not found in {path_str}",
                )
            if not replace_all and count > 1:
                return ToolResult(
                    success=False,
                    output=(
                        f"old_string appears {count} times in {path_str}; "
                        "it must be unique or set replace_all=true"
                    ),
                )
            new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            data = new_text.encode("utf-8")
            os.ftruncate(opened.fd, 0)
            os.lseek(opened.fd, 0, os.SEEK_SET)
            while data:
                written = os.write(opened.fd, data)
                data = data[written:]
            post = os.fstat(opened.fd)
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(success=False, output=f"edit error: {exc}")
        finally:
            os.close(opened.fd)

        self._tracker.record_stat(target, mtime=post.st_mtime, size=post.st_size)
        replaced = count if replace_all else 1
        return ToolResult(
            success=True,
            output=f"replaced {replaced} occurrence(s) in {path_str}",
            artifacts=(
                ToolArtifact(
                    path=opened.path.relative_to(self._sandbox.workspace).as_posix()
                ),
            ),
        )


__all__ = ["EditTool"]
