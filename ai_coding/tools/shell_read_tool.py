"""``shell_read``: the only shell plan mode may use, and only for reads.

Plan mode needs ``git log`` / ``find`` / ``wc``; ``read/grep/glob`` cannot answer
those.  Opening ``bash`` would open every command, so this tool runs the same
process sandbox as bash but admits only one simple, allow-listed argv command
(see :mod:`ai_coding.security.read_only_shell`).  It is plan-only: outside the
planning posture the ordinary ``bash`` tool is the one to use.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.read_only_shell import read_only_problem
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.shell_parse import path_candidates
from ai_coding.tools.base import ToolContext, resolve_ctx
from ai_coding.tools.bash_tool import BashTool

__all__ = ["ShellReadTool"]


class ShellReadTool(BashTool):
    """Read-only subset of bash, advertised only in plan mode."""

    plan_only = True
    mutates_world = False
    parallel_safe = True

    @property
    def name(self) -> str:
        return "shell_read"

    @property
    def description(self) -> str:
        return (
            "Run one READ-ONLY shell command for inspection (plan mode only): "
            "git status/log/diff/show/branch/rev-parse/ls-files/blame, ls, pwd, "
            "wc, find (without -delete/-exec), stat, file, tree, du, df. "
            "No pipes, redirection, command substitution, variables or chaining; "
            "one simple command at a time.  For file contents use read/grep."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "One read-only shell command.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Optional per-call timeout.",
                },
            },
            "required": ["command"],
        }

    def precheck(self, params: dict[str, Any]) -> ToolResult | None:
        problem = read_only_problem(str(params.get("command") or ""))
        if problem:
            return ToolResult(
                success=False,
                output=f"error: read-only shell refused: {problem}",
                error="read_only_shell_required",
            )
        return None

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        if params.get("run_in_background"):
            return ToolResult(
                success=False,
                output="error: shell_read cannot run in the background",
                error="read_only_shell_required",
            )
        problem = read_only_problem(str(params.get("command") or ""))
        if problem:
            return ToolResult(
                success=False,
                output=f"error: read-only shell refused: {problem}",
                error="read_only_shell_required",
            )
        context = resolve_ctx(ctx)
        command = str(params.get("command") or "")
        try:
            sandbox = Sandbox(
                context.workspace or ".", extra_roots=context.extra_workspace_roots
            )
            for candidate in path_candidates(command):
                sandbox.resolve(candidate)
        except SandboxViolation as exc:
            return ToolResult(
                success=False,
                output=f"error: path outside the workspace: {exc}",
                error="sandbox_violation",
            )
        return await super().execute(params, ctx)
