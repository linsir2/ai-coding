"""Codex-shaped multi-agent tools (C-5a).

Four model-visible tools over the existing :class:`~ai_coding.core.subagent.SubAgentRunner`:
``spawn_agent`` starts a background child and returns its id at once, ``wait_agent``
observes one run (it never cancels it), ``list_agents`` reads the background table
and ``close_agent`` cancels one run.  The runner keeps owning the semaphore, the
worktree, the timeout and the ledger; these tools only parse, map and render.

The completion lane is unchanged: a settled child announces itself through the
runner's ``notices`` (user + synthetic), so ``wait_agent`` deliberately returns a
status line rather than the child's prose — the transcript already gets it.
"""

from __future__ import annotations

import time
from typing import Any

from ai_coding.core.observers import EmitObserver
from ai_coding.domain.message import ToolResult
from ai_coding.domain.subagent import ParentContext
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = [
    "CloseAgentTool",
    "ListAgentsTool",
    "SpawnAgentTool",
    "WaitAgentTool",
]

#: ``wait_agent`` bounds (C-5a): below a second a wait cannot observe anything,
#: above ten minutes it outlives the turn it serves.
_MIN_WAIT_MS = 1_000
_MAX_WAIT_MS = 600_000
_DEFAULT_WAIT_MS = 30_000


class SpawnAgentTool(BaseTool):
    """Start a background sub-agent and hand back its run id immediately."""

    #: Spawning writes the background table, so it is not sibling-safe.
    parallel_safe = False
    #: The child does not touch the parent workspace directly.
    mutates_world = False

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
        return "spawn_agent"

    @property
    def description(self) -> str:
        return (
            "Start a sub-agent in the background and return its id at once; "
            "the parent turn keeps going. 'fork_turns' chooses what the child "
            "starts from: 'none' (default) gives it only the task text, 'all' "
            "gives it this conversation's messages and rules. Use 'name' for a "
            "short label. The child's completion arrives on its own as a "
            "notice; wait_agent observes one, list_agents shows the table and "
            "close_agent cancels one."
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
                "fork_turns": {
                    "type": "string",
                    "enum": ["none", "all"],
                    "description": (
                        "'none' starts from a clean context: put every fact, "
                        "path and decision the child needs into 'task'. "
                        "'all' starts from this conversation's messages."
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
        name = str(params.get("name", "") or "").strip() or "sub-agent"
        fork = str(params.get("fork_turns", "") or "none").strip().lower()
        if fork not in ("none", "all"):
            fork = "none"

        context = resolve_ctx(ctx)
        parent: ParentContext | None = None
        context_mode = "task_only"
        if fork == "all":
            # r25/v2: only the request view and the rule layer travel; the child
            # assembles its own system prompt either way.
            parent = ParentContext(
                messages=tuple(context.messages),
                permissions=tuple(context.permissions),
            )
            context_mode = "full"

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
                context_mode=context_mode,
            )
        except RuntimeError as exc:
            return ToolResult(
                success=False,
                output=f"agent not started: {exc}",
                error="agents_exhausted",
            )
        return ToolResult(
            success=True,
            output=f"spawned {name} as {run_id}",
            data={"agent_id": run_id, "status": "running", "fork_turns": fork},
        )


class WaitAgentTool(BaseTool):
    """Wait for one background run and report its status (never its prose)."""

    #: Observes a table row; the wait itself changes nothing.
    parallel_safe = False
    mutates_world = False

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "wait_agent"

    @property
    def description(self) -> str:
        return (
            "Wait for one background sub-agent by id and return its status line "
            "(running / completed / timeout / error / cancelled). It never "
            "cancels the run and never repeats the child's conclusion — that "
            "arrives as a notice. 'timeout_ms' is clamped to 1s..10min "
            "(default 30s); a timed-out wait just means the child is still "
            "running."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The id returned by spawn_agent.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": (
                        "How long to wait, in milliseconds (default 30000; "
                        "clamped to 1000..600000)."
                    ),
                },
            },
            "required": ["agent_id"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        agent_id = str(params.get("agent_id", "") or "").strip()
        if not agent_id:
            return ToolResult(
                success=False,
                output="error: 'agent_id' is required",
                error="missing_agent_id",
            )
        timeout_ms = _clamp_timeout(params.get("timeout_ms"))
        status, elapsed_ms = await self._runner.wait(agent_id, timeout_ms)
        output = f"agent {agent_id}: {status} ({elapsed_ms} ms)"
        data = {"agent_id": agent_id, "status": status, "elapsed_ms": elapsed_ms}
        if status == "unknown":
            return ToolResult(
                success=False, output=output, error="unknown_agent", data=data
            )
        return ToolResult(success=True, output=output, data=data)


class ListAgentsTool(BaseTool):
    """The background table: what is running and what has settled."""

    #: Spawn/close write the table; C-5a keeps the whole face non-sibling-safe.
    parallel_safe = False
    mutates_world = False

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "list_agents"

    @property
    def description(self) -> str:
        return (
            "List every background sub-agent started in this process: id, name, "
            "status and elapsed time, oldest first. Use it to see what is still "
            "running and to find an id whose completion notice you no longer "
            "have. The table lives in memory and does not survive a restart."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        rows = self._runner.task_list()
        if not rows:
            return ToolResult(
                success=True,
                output="no background agents",
                data={"agents": []},
            )
        now = time.monotonic()
        lines = [
            f"- {row.run_id} {row.name} [{row.status}] "
            f"{(row.ended if row.ended is not None else now) - row.started:.0f}s"
            for row in rows
        ]
        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={"agents": [row.to_dict() for row in rows]},
        )


class CloseAgentTool(BaseTool):
    """Cancel one background run."""

    parallel_safe = False
    mutates_world = False

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "close_agent"

    @property
    def description(self) -> str:
        return (
            "Cancel a running background sub-agent by id. It reports whether the "
            "run was still live; a settled or unknown id changes nothing. The "
            "child's already-written commit/branch and ledger stay as they are."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The id returned by spawn_agent.",
                },
            },
            "required": ["agent_id"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        agent_id = str(params.get("agent_id", "") or "").strip()
        if not agent_id:
            return ToolResult(
                success=False,
                output="error: 'agent_id' is required",
                error="missing_agent_id",
            )
        closed = bool(self._runner.cancel(agent_id))
        return ToolResult(
            success=True,
            output=f"closed {agent_id}" if closed else f"not running: {agent_id}",
            data={"agent_id": agent_id, "closed": closed},
        )


def _clamp_timeout(raw: Any) -> int:
    """The wait bound actually used: default 30s, clamped to 1s..10min."""
    try:
        value = int(raw) if raw is not None else _DEFAULT_WAIT_MS
    except (TypeError, ValueError):
        value = _DEFAULT_WAIT_MS
    return max(_MIN_WAIT_MS, min(_MAX_WAIT_MS, value))
