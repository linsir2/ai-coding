"""Read tool — read a text file with line numbers and optional offset/limit."""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.base import BaseTool

_DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


class ReadTool(BaseTool):
    """Read a text file with line numbers; supports offset/limit pagination."""

    def __init__(
        self,
        sandbox: Sandbox,
        file_state_tracker: FileStateTracker,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker
        self._max_file_size = max_file_size

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a text file with line numbers. "
            "Use offset and limit for large files. "
            f"Max file size: {self._max_file_size} bytes."
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
                "offset": {
                    "type": "integer",
                    "description": "0-based start line (default 0).",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to read (default all).",
                },
            },
            "required": ["path"],
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        path_str = str(params.get("path", ""))
        offset = int(params.get("offset", 0) or 0)
        limit = params.get("limit")
        limit = int(limit) if limit is not None else None

        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc))

        if not target.is_file():
            return ToolResult(success=False, output=f"file not found: {path_str}")

        try:
            size = target.stat().st_size
        except OSError as exc:
            return ToolResult(success=False, output=f"cannot stat file: {exc}")

        if size > self._max_file_size:
            msg = f"file too large ({size} bytes > {self._max_file_size} limit)"
            return ToolResult(success=False, output=msg)

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(success=False, output=f"read error: {exc}")

        lines = text.splitlines(keepends=False)
        end = offset + limit if limit is not None else len(lines)
        view = lines[offset:end]

        # line-numbered output, right-aligned 6-wide like `cat -n`
        numbered = [f"{i + 1:>6}  {line}" for i, line in enumerate(view, start=offset)]
        output = "\n".join(numbered)
        if offset > 0 or (limit is not None and end < len(lines)):
            output += f"\n[showing lines {offset + 1}-{end} of {len(lines)}]"

        # record snapshot for stale-read protection
        self._tracker.record(target)

        return ToolResult(success=True, output=output)


__all__ = ["ReadTool"]
