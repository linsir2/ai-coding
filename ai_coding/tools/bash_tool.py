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
# Per-call widening ceiling: the model may raise a slow command's timeout up to
# this (see `timeout_seconds` in the schema), but never past it.  It lines up
# with the turn wall clock (`ai.turn_timeout_seconds`, 600) and the sub-agent
# ceiling, so one call can never eat more than a turn.  The dispatcher still
# takes min(tool ceiling, turn remaining), so this is a cap, not a promise.
_MAX_TIMEOUT_SECONDS = 600
# The dispatcher's per-call ceiling sits above the command timeout so the runner
# (not the budget watchdog) is what normally stops a runaway command.
_TIMEOUT_GRACE = 15.0


class BashTool(BaseTool):
    """Execute shell commands and return stdout+stderr as output."""

    def __init__(
        self,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT,
        max_timeout_seconds: int = _MAX_TIMEOUT_SECONDS,
    ) -> None:
        self._max_timeout = max(1, int(max_timeout_seconds))
        # The default can never exceed the ceiling: two yardsticks for one
        # command would teach the model that limits are negotiable prose.
        self._timeout = max(1, min(int(timeout_seconds), self._max_timeout))
        self._max_output = max_output_chars

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Run a shell command and return its output. "
            "The command runs in the workspace directory. "
            f"Timeout is {self._timeout}s by default; pass timeout_seconds "
            f"(up to {self._max_timeout}s) to widen it for one slow call. "
            f"Output is truncated after {self._max_output} characters."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                # The self-widening knob: when a long command times out, the
                # model retries with a larger value instead of wrapping the
                # command in `timeout(1)` (whose margin it used to guess from
                # the description above, and guess short).  A new value is a
                # new guard fingerprint, so widening retries are not denied as
                # duplicate calls.
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Per-call timeout in seconds "
                        f"(1..{self._max_timeout}). Defaults to "
                        f"{self._timeout}. Set higher for long commands "
                        "(full test suites, builds); if a run times out, "
                        "retry with a larger value instead of wrapping the "
                        "command in `timeout(1)`."
                    ),
                },
            },
            "required": ["command"],
        }

    @property
    def max_duration_seconds(self) -> float:
        """Per-call ceiling for the dispatcher (R2a): the *widest* timeout the
        tool will honour, plus grace — not the default.  Otherwise a call the
        model legitimately widened would be killed by the dispatcher first and
        the knob would be a lie."""
        return self._max_timeout + _TIMEOUT_GRACE

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        command = str(params.get("command", ""))
        if not command.strip():
            return ToolResult(success=False, output="error: empty command")

        # Self-widening: the model names its own budget per call.  Strict on
        # purpose — a bool, a string or a non-positive number is a contract
        # misunderstanding, and silently accepting it would teach the model
        # that the knob is decorative.
        requested = params.get("timeout_seconds", None)
        if requested is None:
            effective = self._timeout
        elif (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or requested < 1
        ):
            return ToolResult(
                success=False,
                output=(
                    f"error: invalid timeout_seconds={requested!r}; "
                    f"pass an integer number of seconds (1..{self._max_timeout})"
                ),
                error="invalid_params",
                data={"timeout_seconds": requested},
            )
        else:
            effective = min(requested, self._max_timeout)

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
            timeout=effective,
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
                data={
                    "duration_seconds": round(outcome.duration_seconds, 3),
                    "timeout_seconds": effective,
                },
            )

        output = (outcome.stdout + outcome.stderr) if outcome.stderr else outcome.stdout
        data: dict[str, Any] = {
            "exit_code": outcome.returncode,
            "timed_out": outcome.timed_out,
            "duration_seconds": round(outcome.duration_seconds, 3),
            # Observability for the widening loop (and its tests): what budget
            # this call actually ran under.  Additive only — the error codes
            # and every other key are untouched.
            "timeout_seconds": effective,
        }

        if outcome.timed_out:
            # Partial output is kept: it often says where the command got stuck.
            # The note teaches the exit: widen and retry while headroom
            # remains, split or background once the ceiling is reached.  This
            # text is the loop's only way out, so it names both the knob and
            # the number — a bare "timed out" is what the model used to poll
            # against until the iteration budget died.
            if effective < self._max_timeout:
                note = (
                    f"timeout: command exceeded {effective}s limit; retry with "
                    f"a larger timeout_seconds (up to {self._max_timeout}s)"
                )
            else:
                note = (
                    f"timeout: command exceeded {effective}s limit (already at "
                    f"the {self._max_timeout}s ceiling); split the command, "
                    "narrow its scope, or run it in the background and poll"
                )
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
