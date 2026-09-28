"""Bash tool — run shell commands with a hard timeout and output truncation.

R2b: execution goes through :mod:`ai_coding.infra.subprocess_runner` (thread +
process-group kill) instead of ``asyncio.create_subprocess_shell``, whose
timeout could never fire while a child held the pipe (docs/r2-analysis.md 1.4-A).
The command runs in ``ctx.workspace`` — the description always promised that,
but the old implementation silently inherited the process CWD, which would send
a sub-agent's shell writes into the parent's tree.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from ai_coding.core.events import ToolChunk
from ai_coding.domain.message import ToolResult
from ai_coding.infra.subprocess_runner import run_process_async
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

_DEFAULT_TIMEOUT = 60
_DEFAULT_MAX_OUTPUT = 50_000
# The dispatcher's per-call ceiling sits above the command timeout so the runner
# (not the budget watchdog) is what normally stops a runaway command.
_TIMEOUT_GRACE = 15.0


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

    @property
    def max_duration_seconds(self) -> float:
        """Per-call ceiling for the dispatcher (R2a): the command timeout + grace."""
        return self._timeout + _TIMEOUT_GRACE

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        command = str(params.get("command", ""))
        if not command.strip():
            return ToolResult(success=False, output="error: empty command")

        # R8c: stream what the command says while it says it.  Chunks are
        # observation only - the ToolResult below is what the model ever sees.
        counter = itertools.count()
        live = resolve_ctx(ctx)

        def _chunk(stream: str, text: str) -> None:
            live.emit(
                ToolChunk(
                    call_id=live.call_id,
                    tool_name="bash",
                    stream=stream,
                    text=text,
                    seq=next(counter),
                )
            )

        outcome = await run_process_async(
            command,
            shell=True,
            cwd=self._workspace_dir(ctx),
            timeout=self._timeout,
            max_output_chars=self._max_output,
            on_chunk=_chunk,
            # R8c: when the user cancels, the dispatcher sets this flag and the
            # runner kills the process group - waiting is not enough (D-10).
            cancel=live.kill_event,
        )

        if outcome.error is not None:
            return ToolResult(
                success=False,
                output=f"failed to start command: {outcome.error}",
                error="spawn_failed",
                data={"duration_seconds": round(outcome.duration_seconds, 3)},
            )

        output = (outcome.stdout + outcome.stderr) if outcome.stderr else outcome.stdout
        data: dict[str, Any] = {
            "exit_code": outcome.returncode,
            "timed_out": outcome.timed_out,
            "duration_seconds": round(outcome.duration_seconds, 3),
        }

        if outcome.timed_out:
            # Partial output is kept: it often says where the command got stuck.
            note = f"timeout: command exceeded {self._timeout}s limit"
            return ToolResult(
                success=False,
                output=f"{note}\n{output}" if output else note,
                error="timeout",
                data=data,
            )

        if len(output) > self._max_output:
            output = output[: self._max_output] + (
                f"\n... [truncated, total {len(output)} chars]"
            )

        if outcome.returncode == 0:
            return ToolResult(success=True, output=output, data=data)
        return ToolResult(
            success=False,
            output=output or f"command exited with code {outcome.returncode}",
            error="nonzero_exit",
            data=data,
        )

    @staticmethod
    def _workspace_dir(ctx: ToolContext | None) -> str | None:
        """``ctx.workspace`` when it is a real directory, else inherit the CWD."""
        workspace = resolve_ctx(ctx).workspace
        if workspace and Path(workspace).is_dir():
            return workspace
        return None


__all__ = ["BashTool"]
