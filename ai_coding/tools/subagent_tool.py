"""subAgent tool — skeleton for delegating work to a sub-agent.

M2 implementation: reuses the same AI service (model) to run a sub-conversation.
M3 will add Git Worktree isolation, background mode, and proper handoff history.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool


class SubAgentTool(BaseTool):
    """Delegate a task to a sub-agent (skeleton implementation)."""

    def __init__(self, ai_service: Any) -> None:
        self._ai = ai_service

    @property
    def name(self) -> str:
        return "subAgent"

    @property
    def description(self) -> str:
        return (
            "Delegate a task to a sub-agent. The sub-agent runs independently and "
            "returns its final answer. Use 'task' for what to do, and optionally "
            "'name' for a short agent label."
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

        prompt = (
            f"You are {agent_name}, a sub-agent. "
            f"Complete the following task and return your final answer:\n\n{task}"
        )

        try:
            turn = await self._ai.execute_turn([], prompt)
        except Exception as exc:  # noqa: BLE001 — broad catch for tool boundary
            return ToolResult(success=False, output=f"sub-agent failed: {exc}")

        output = turn.text or "(sub-agent produced no output)"
        return ToolResult(
            success=True,
            output=f"[{agent_name}]\n{output}",
        )


__all__ = ["SubAgentTool"]
