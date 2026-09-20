"""Glob tool — find files by name pattern."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.base import BaseTool

_DEFAULT_MAX_RESULTS = 250
_DEFAULT_MAX_DEPTH = 20


class GlobTool(BaseTool):
    """Search for files matching a glob pattern, sorted by modification time (newest first)."""

    def __init__(
        self,
        sandbox: Sandbox,
        max_results: int = _DEFAULT_MAX_RESULTS,
        max_depth: int = _DEFAULT_MAX_DEPTH,
    ) -> None:
        self._sandbox = sandbox
        self._max_results = max_results
        self._max_depth = max_depth

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Search for files matching a glob pattern (e.g. '**/*.py'). "
            f"Results are sorted by mtime (newest first), max {self._max_results} results."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, relative to workspace.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return.",
                    "default": _DEFAULT_MAX_RESULTS,
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        pattern = str(params.get("pattern", ""))
        max_results = int(params.get("max_results", self._max_results) or self._max_results)

        if not pattern:
            return ToolResult(success=False, output="pattern must not be empty")

        # Reject patterns that try to escape the workspace (defense in depth)
        if ".." in pattern.split("/"):
            return ToolResult(
                success=False,
                output="glob pattern must not contain '..' (workspace escape blocked)",
            )

        try:
            # resolve the pattern's starting directory to ensure it's inside sandbox
            # For simple patterns like "*.py" the start is workspace itself.
            # For patterns with ".." we need to validate.
            start = self._sandbox.resolve(".")
            # Use Path.glob which operates relative to start
            matches: list[Path] = list(start.glob(pattern))
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc))
        except (ValueError, OSError) as exc:
            return ToolResult(success=False, output=f"glob error: {exc}")

        # Filter: only files, only inside workspace (defense in depth)
        files: list[Path] = []
        for m in matches:
            try:
                resolved = m.resolve()
                resolved.relative_to(self._sandbox.workspace)
            except (OSError, ValueError):
                continue
            if m.is_file():
                files.append(resolved)

        # Depth limit
        files = [
            f for f in files
            if len(f.relative_to(self._sandbox.workspace).parts) <= self._max_depth
        ]

        # Sort by mtime descending
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            # if stat fails, leave unsorted
            pass

        # Truncate
        total = len(files)
        shown = files[:max_results]

        # Format output: relative paths
        lines = [
            str(p.relative_to(self._sandbox.workspace)) for p in shown
        ]
        if total > max_results:
            lines.append(f"... [showing {max_results} of {total} results]")

        return ToolResult(success=True, output="\n".join(lines) if lines else "(no matches)")


__all__ = ["GlobTool"]
