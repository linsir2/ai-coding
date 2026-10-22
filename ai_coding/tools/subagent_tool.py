"""subAgent tool (R1a-5) — delegate with the parent's context.

The tool passes the parent turn's request view and system prompt to the runner
(so the sub-agent starts from the same place the parent was looking from) and
turns the returned packet into a ``ToolResult`` with both readable text and
machine-readable ``data``.  The sub-agent's reasoning is not returned.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ai_coding.core.observers import EmitObserver
from ai_coding.domain.message import ToolResult
from ai_coding.domain.subagent import ParentContext, SubagentResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

SUCCESS_STATUSES = frozenset({"completed", "max_iterations"})

#: Bound on a caller-supplied ``output_schema`` (R1-3): the schema is pasted
#: into the child's prompt, so an unbounded one is a token bomb.  4000 chars
#: fits any honest contract (a dozen required keys with descriptions).
_MAX_SCHEMA_CHARS = 4000

#: Machine-readable child termination codes (R0-3): the parent must be able
#: to tell "done" from "cut" without parsing prose (measured: C1's timeout
#: arrived with ``error=None``).
CHILD_ERRORS = {
    "timeout": "child_timeout",
    "max_iterations": "child_max_iterations",
    "error": "child_error",
}

# Room above the runner's own budget for worktree setup, ledger write and the
# packet itself (R2a).  The dispatcher takes min(this, remaining turn budget).
_TIMEOUT_GRACE = 15.0


def _render(result: SubagentResult, name: str) -> str:
    """Human/LLM-readable rendering of the packet."""
    lines = [f"[{name}] {result.status} in {result.iterations} iteration(s)"]
    if result.truncated:
        lines.append(f"⚠ truncated: {result.exit_reason or 'unfinished work remains'}")
    if result.usage:
        prompt = result.usage.get("prompt_tokens", 0)
        completion = result.usage.get("completion_tokens", 0)
        lines.append(f"tokens: prompt={prompt} completion={completion}")
    if result.conclusion:
        lines.append(result.conclusion)
    elif result.error:
        lines.append(f"error: {result.error}")
    # R1-3: the machine verdict on the conclusion's shape.  A valid packet
    # gets one compact facts line; an invalid one names the miss but keeps
    # the raw text above — the work stands, only the shape failed.
    if result.schema_valid is True:
        facts = result.facts or {}
        parts = [f"{key}={str(value)[:80]}" for key, value in list(facts.items())[:8]]
        lines.append("facts: " + (", ".join(parts) if parts else "(empty)"))
    elif result.schema_valid is False:
        miss = "; ".join(result.schema_errors) or "unparseable conclusion"
        lines.append(f"⚠ 结构校验未过 ({miss})，已保留原文")

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
    # R0-3: the error is always a machine code for non-completed statuses —
    # C1's timeout arrived with a free-text error the parent could only parse
    # by reading.  The human detail stays in the output and ``error_detail``.
    error = None
    error_detail = result.error
    if result.status != "completed":
        error = CHILD_ERRORS.get(result.status, "child_error")
    return ToolResult(
        # A partial result (max_iterations) still beats discarding the work.
        success=result.status in SUCCESS_STATUSES,
        output=_render(result, name),
        error=error,
        data={
            "status": result.status,
            "iterations": result.iterations,
            "todos": [dict(entry) for entry in result.todos],
            "artifacts": [artifact.to_dict() for artifact in result.artifacts],
            "ledger_path": result.ledger_path,
            "workdir": result.workdir,
            "error": error,
            "error_detail": error_detail,
            "truncated": result.truncated,
            "exit_reason": result.exit_reason,
            "usage": dict(result.usage) if result.usage is not None else None,
            "conclusion_chars": len(conclusion) if conclusion else 0,
            "facts": dict(result.facts) if result.facts is not None else None,
            "schema_valid": result.schema_valid,
            "schema_errors": list(result.schema_errors),
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
        # R7e-4: say which window the child gets.  In task_only mode the model is
        # the only one who can hand the facts over, so the description asks for it.
        if getattr(self._runner, "context_mode", "full") == "task_only":
            window = (
                "starts with a CLEAN context: it cannot see this conversation, so put "
                "every fact, path and decision it needs into 'task'"
            )
        else:
            window = "sees the same conversation context and system prompt as you do"
        return (
            f"Delegate a task to a sub-agent that {window}, then returns a "
            "structured packet: its conclusion, any unfinished task items, changed "
            "files, and a ledger path holding the full report. Use 'task' for what "
            "to do, 'agent' to choose which agent runs it (an id from the agent "
            "table; a primary agent cannot be spawned), and 'name' for a short "
            "label you make up. The sub-agent's own reasoning is not returned."
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
                    "description": (
                        "Optional short label for this run (you choose it; it is "
                        "not an agent id)."
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Optional subagent id from the agent table. Defaults to "
                        "general. Primary agents cannot be spawned."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run without waiting: returns a run id at once, the "
                        "child runs on, and its completion lands as an event "
                        "plus a subagent_list row. For long work the parent "
                        "would otherwise wait out its whole turn budget."
                    ),
                },
                "output_schema": {
                    "type": "object",
                    "description": (
                        "JSON-shape contract for the sub-agent's conclusion "
                        "(R1-3): the child is instructed to return its verdict "
                        "as a JSON object, and the packet carries the parsed "
                        "'facts' plus a 'schema_valid' verdict. Only require "
                        "fields you will actually read. Only 'required' is "
                        "enforced; other JSON Schema keywords are ignored. "
                        "A bad shape never sinks the work — the raw "
                        "conclusion is always kept."
                    ),
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
        # M1b: forward what the model asked for, verbatim.  ``None`` means "not
        # specified" and the runner resolves the default — the tool does not
        # hold the agent table, so it cannot tell which target is the default.
        agent = params.get("agent")
        agent = str(agent).strip() if isinstance(agent, str) and agent.strip() else None

        # R1-3: validate the shape contract before the runner ever sees it.
        schema = params.get("output_schema")
        if schema is not None:
            if not isinstance(schema, dict):
                return ToolResult(
                    success=False,
                    output="error: 'output_schema' must be a JSON object",
                    error="schema_invalid",
                )
            if len(json.dumps(schema, ensure_ascii=False)) > _MAX_SCHEMA_CHARS:
                return ToolResult(
                    success=False,
                    output=(
                        "error: 'output_schema' is too large "
                        f"(>{_MAX_SCHEMA_CHARS} chars): it is pasted into the "
                        "child's prompt"
                    ),
                    error="schema_too_large",
                )

        context = resolve_ctx(ctx)
        parent: ParentContext | None = None
        if context.messages or context.instructions or context.permissions:
            parent = ParentContext(
                messages=tuple(context.messages),
                instructions=context.instructions,
                permissions=tuple(context.permissions),
            )

        if params.get("background"):
            return self._spawn_background(task, name, context, parent, schema, agent)
        try:
            result = await self._runner.run(
                task,
                name,
                worktree_manager=self._worktree_manager,
                isolate=self._isolate,
                parent=parent,
                # R4a: the trace hangs the child's spans under this call.
                parent_span=context.call_id,
                # R0-3: fan the child's events into the parent stream through
                # the context emit.  The runner wraps this in a ScopedObserver,
                # so the parent trace finally sees child spans (previously the
                # slot was never filled: zero scoped events over 23 runs).
                observer=EmitObserver(context.emit),
                # R1-3: the shape contract, always passed (None = prose as
                # usual) so the call shape is one stable contract.
                output_schema=schema,
                agent=agent,
            )
        except Exception as exc:  # noqa: BLE001 — tool boundary
            return ToolResult(
                success=False,
                output=f"sub-agent failed: {exc}",
                error="subagent_error",
            )
        return _to_tool_result(result, name)

    def _spawn_background(
        self, task: str, name: str, context: ToolContext, parent: ParentContext | None,
        schema: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> ToolResult:
        """Hand a child to the background and return its handle at once.

        The spawning turn pays nothing past this call — no waiting, no
        budget burn.  Child events fan into the parent stream through the
        context emit (R0-3 wiring); completion lands as an event plus a
        table row, never as a second tool message (strict pairing allows
        exactly one tool message per call).
        """
        try:
            run_id = self._runner.spawn_background(
                task,
                name,
                worktree_manager=self._worktree_manager,
                isolate=self._isolate,
                parent=parent,
                parent_span=context.call_id,
                observer=EmitObserver(context.emit),
                emit=context.emit,
                origin_session=context.session_id,
                # R1-3: the shape contract rides with the supervised run —
                # facts land in the packet and its ledger file, pointed at by
                # the completion event.
                output_schema=schema,
                agent=agent,
            )
        except RuntimeError as exc:
            return ToolResult(
                success=False,
                output=f"sub-agent not started: {exc}",
                error="subagent_busy",
            )
        return ToolResult(
            success=True,
            output=(
                f"[{name}] started in background as {run_id} — the result "
                "arrives on its own when it settles; subagent_list shows what "
                "is still running"
            ),
            data={"run_id": run_id, "background": True, "status": "running"},
        )

    @property
    def runner(self) -> Any:
        """The runner behind this tool (``/tasks`` reads its table)."""
        return self._runner


#: C-3b: how many settled rows a listing shows before it collapses the rest
#: into a count line (job_list's rule, same number).
_SETTLED_ROWS = 20


class SubAgentListTool(BaseTool):
    """What background runs exist: still running, settled, and where the
    settled ones' full packets live."""

    #: Reading a status board changes nothing.
    parallel_safe = True
    mutates_world = False

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "subagent_list"

    @property
    def description(self) -> str:
        return (
            "List the background sub-agent runs started in this process: id, "
            "name, status, how long, token spend, the error if one settled "
            "badly, and the ledger path holding a settled run's full packet. "
            "Use it to see what is still running and to find the id or the "
            "packet of a run whose completion notice you no longer have — a "
            "settled run's result also arrives on its own. The table lives in "
            "memory and does not survive a restart."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        rows = self._runner.task_list()
        if not rows:
            return ToolResult(success=True, output="no background sub-agent run")

        now = time.monotonic()
        running = [row for row in rows if row.status == "running"]
        # task_list() is oldest-first; a reader looking for "what happened"
        # wants the newest settled rows first (job_list's shape).
        settled = [row for row in reversed(rows) if row.status != "running"]
        older = max(0, len(settled) - _SETTLED_ROWS)
        shown = running + settled[: _SETTLED_ROWS]

        lines: list[str] = []
        for row in shown:
            elapsed = (row.ended if row.ended is not None else now) - row.started
            line = f"{row.run_id[:12]} {row.name} {row.status} {elapsed:.0f}s"
            if row.usage:
                total = int(row.usage.get("total_tokens", 0) or 0)
                if not total:
                    total = int(row.usage.get("prompt_tokens", 0) or 0) + int(
                        row.usage.get("completion_tokens", 0) or 0
                    )
                if total:
                    line += f" · {total} tok"
            if row.error:
                line += f" · {row.error}"
            if row.ledger_path:
                line += f" · 完整回包 {row.ledger_path}"
            lines.append(line)
        if older:
            lines.append(f"(还有 {older} 条更早的已结算 run 未显示)")

        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={
                "runs": [
                    {
                        "id": row.run_id,
                        "name": row.name,
                        "status": row.status,
                        "ledger_path": row.ledger_path,
                    }
                    for row in shown
                ],
                "shown": len(shown),
                "older_settled": older,
            },
        )


__all__ = ["SubAgentListTool", "SubAgentTool"]
