"""Edit tool — precise string replacement with stale-read protection."""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.file_state_tracker import FileState, FileStateTracker
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.base import BaseTool


class EditTool(BaseTool):
    """Replace a string in a file. ``old_string`` must match exactly once."""

    def __init__(
        self,
        sandbox: Sandbox,
        file_state_tracker: FileStateTracker,
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker

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

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        path_str = str(params.get("path", ""))
        old = str(params.get("old_string", ""))
        new = str(params.get("new_string", ""))
        replace_all = bool(params.get("replace_all", False))

        if not old:
            return ToolResult(success=False, output="old_string must not be empty")

        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc))

        if not target.is_file():
            return ToolResult(success=False, output=f"file not found: {path_str}")

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
        return ToolResult(success=True, output=f"replaced {replaced} occurrence(s) in {path_str}")


__all__ = ["EditTool"]
