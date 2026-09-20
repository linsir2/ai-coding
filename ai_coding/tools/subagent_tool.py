"""subAgent tool (M4-1) — delegate a task to a real sub-agent.

The tool delegates to an injected :class:`SubAgentRunner` and forwards an
optional :class:`WorktreeManager` plus an isolation flag.  The sub-agent runs
its own conversation (isolated in a git worktree when enabled) and only the
final conclusion is returned here as tool output — intermediate tool activity
is discarded by the runner.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool


class SubAgentTool(BaseTool):
    """Delegate a task to a sub-agent through a ``SubAgentRunner``."""

    def __init__(
        self,
        runner: Any,
        worktree_manager: Any = None,
        isolate: bool = True,
    ) -> None:
        self._runner = runner
        self._worktree_manager = worktree_manager
        self._isolate = isolate

    @property
    def name(self) -> str:
        return "subAgent"

    @property
    def description(self) -> str:
        return (
            "Delegate a task to a sub-agent. The sub-agent runs independently "
            "and returns its final answer. Use 'task' for what to do, and "
            "optionally 'name' for a short agent label."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description for the sub-agent.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the sub-agent.",
                },
            },
            "required": ["task"],
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        task = str(params.get("task", "")).strip()
        if not task:
            return ToolResult(success=False, output="error: 'task' is required")
        agent_name = str(params.get("name", "sub-agent")).strip() or "sub-agent"

        try:
            result = await self._runner.run(
                task,
                agent_name,
                worktree_manager=self._worktree_manager,
                isolate=self._isolate,
            )
        except Exception as exc:  # noqa: BLE001 — broad catch for tool boundary
            return ToolResult(success=False, output=f"sub-agent failed: {exc}")

        output = result.text or "(sub-agent produced no output)"
        return ToolResult(
            success=True,
            output=f"[{agent_name}]\n{output}",
        )


__all__ = ["SubAgentTool"]
