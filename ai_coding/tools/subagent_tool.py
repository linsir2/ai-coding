"""subAgent tool (R1a-5) — delegate with the parent's context.

The tool passes the parent turn's request view and system prompt to the runner
(so the sub-agent starts from the same place the parent was looking from) and
turns the returned packet into a ``ToolResult`` with both readable text and
machine-readable ``data``.  The sub-agent's reasoning is not returned.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.domain.subagent import ParentContext, SubagentResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

SUCCESS_STATUSES = frozenset({"completed", "max_iterations"})

# Room above the runner's own budget for worktree setup, ledger write and the
# packet itself (R2a).  The dispatcher takes min(this, remaining turn budget).
_TIMEOUT_GRACE = 15.0


def _render(result: SubagentResult, name: str) -> str:
    """Human/LLM-readable rendering of the packet."""
    lines = [f"[{name}] {result.status} in {result.iterations} iteration(s)"]
    if result.conclusion:
        lines.append(result.conclusion)
    elif result.error:
        lines.append(f"error: {result.error}")

    open_items = [
        entry
        for entry in result.todos
        if entry.get("status") not in ("completed", "dropped")
    ]
    if open_items:
        lines.append(f"未完成项 ({len(open_items)}):")
        lines.extend(f"  - {entry.get('id')}. {entry.get('content')}" for entry in open_items[:10])
    if result.artifacts:
        lines.append("产物:")
        lines.extend(f"  - {artifact.path}" for artifact in result.artifacts[:10])
    if result.workdir:
        lines.append(f"工作区: {result.workdir}")
    if result.ledger_path:
        lines.append(f"完整回包: {result.ledger_path}")
    return "\n".join(lines)


def _to_tool_result(result: SubagentResult, name: str) -> ToolResult:
    """Split the packet into structured data + rendered text."""
    conclusion = result.conclusion
    return ToolResult(
        # A partial result (max_iterations) still beats discarding the work.
        success=result.status in SUCCESS_STATUSES,
        output=_render(result, name),
        data={
            "status": result.status,
            "iterations": result.iterations,
            "todos": [dict(entry) for entry in result.todos],
            "artifacts": [artifact.to_dict() for artifact in result.artifacts],
            "ledger_path": result.ledger_path,
            "workdir": result.workdir,
            "error": result.error,
            "conclusion_chars": len(conclusion) if conclusion else 0,
        },
        artifacts=result.artifacts,
    )


class SubAgentTool(BaseTool):
    """Delegate a task to a sub-agent through a ``SubAgentRunner``."""

    # Delegated runs are isolated (own worktree when enabled); the runner's
    # semaphore bounds how many actually run at once.
    parallel_safe = True

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
    def max_duration_seconds(self) -> float | None:
        """Per-call ceiling for the dispatcher (R2a)."""
        timeout = getattr(self._runner, "timeout_seconds", None)
        if timeout is None:
            return None
        return float(timeout) + _TIMEOUT_GRACE

    @property
    def description(self) -> str:
        return (
            "Delegate a task to a sub-agent that sees the same conversation "
            "context and system prompt as you do, then returns a structured "
            "packet: its conclusion, any unfinished task items, changed files, "
            "and a ledger path holding the full report. Use 'task' for what to "
            "do and optionally 'name' for a short agent label. The sub-agent's "
            "own reasoning is not returned."
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
                    "description": "Optional short label for the sub-agent.",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        task = str(params.get("task", "")).strip()
        if not task:
            return ToolResult(
                success=False,
                output="error: 'task' is required",
                error="missing_task",
            )
        name = str(params.get("name", "sub-agent")).strip() or "sub-agent"

        context = resolve_ctx(ctx)
        parent: ParentContext | None = None
        if context.messages or context.instructions:
            parent = ParentContext(
                messages=tuple(context.messages),
                instructions=context.instructions,
            )

        try:
            result = await self._runner.run(
                task,
                name,
                worktree_manager=self._worktree_manager,
                isolate=self._isolate,
                parent=parent,
            )
        except Exception as exc:  # noqa: BLE001 — tool boundary
            return ToolResult(
                success=False,
                output=f"sub-agent failed: {exc}",
                error="subagent_error",
            )
        return _to_tool_result(result, name)


__all__ = ["SubAgentTool"]
