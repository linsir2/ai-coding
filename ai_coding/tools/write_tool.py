"""Write tool — write/overwrite a file with stale-read protection."""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.file_state_tracker import FileState, FileStateTracker
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.base import BaseTool

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
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker
        self._max_chars = max_chars

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

    async def execute(self, params: dict[str, Any]) -> ToolResult:
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
            return ToolResult(success=False, output=str(exc))

        # Stale-read check for existing files
        if target.exists():
            state = self._tracker.check(target)
            if state == FileState.NEVER_READ:
                msg = (
                    f"cannot write to '{path_str}': you must read the file first "
                    "(stale-read protection)"
                )
                return ToolResult(success=False, output=msg)
            if state == FileState.STALE:
                msg = (
                    f"cannot write to '{path_str}': file has been modified externally; "
                    "re-read it first"
                )
                return ToolResult(success=False, output=msg)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, output=f"write error: {exc}")

        self._tracker.record(target)
        return ToolResult(success=True, output=f"wrote {len(content)} chars to {path_str}")


__all__ = ["WriteTool"]
