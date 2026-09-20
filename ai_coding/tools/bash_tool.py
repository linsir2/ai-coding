"""Bash tool — run shell commands with timeout and output truncation."""

from __future__ import annotations

import asyncio
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool

_DEFAULT_TIMEOUT = 60
_DEFAULT_MAX_OUTPUT = 50_000


class BashTool(BaseTool):
    """Execute shell commands and return stdout+stderr as output."""

    def __init__(
        self,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_output = max_output_chars

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Run a shell command and return its output. "
            "The command runs in the workspace directory. "
            f"Timeout is {self._timeout}s; output is truncated after "
            f"{self._max_output} characters."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                }
            },
            "required": ["command"],
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        command = str(params.get("command", ""))
        if not command.strip():
            return ToolResult(success=False, output="error: empty command")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    success=False,
                    output=f"timeout: command exceeded {self._timeout}s limit",
                )
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, output=f"failed to start command: {exc}")

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        output = (stdout + stderr) if stderr else stdout

        if len(output) > self._max_output:
            total = len(stdout + stderr)
            output = output[: self._max_output] + f"\n... [truncated, total {total} chars]"

        if proc.returncode == 0:
            return ToolResult(success=True, output=output or "")
        return ToolResult(
            success=False,
            output=output or f"command exited with code {proc.returncode}",
        )


__all__ = ["BashTool"]
