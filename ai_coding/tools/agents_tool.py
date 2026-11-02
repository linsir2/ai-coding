"""Codex-shaped multi-agent tools (C-5a / C-5b).

The model-visible face of :class:`~ai_coding.core.subagent.SubAgentRunner`:
``spawn_agent`` starts a background child and returns its id at once, ``wait_agent``
observes one run (it never cancels it), ``list_agents`` reads the background table,
``close_agent`` closes one run and the C-5b trio adds turn-level control:
``send_message`` (delivered to a running turn or wakes an idle child),
``interrupt_agent`` (cancels the current turn, keeps the handle) and
``resume_agent`` (reopens the same conversation).  The runner keeps owning the
semaphore, the worktree, the timeout and the ledger; these tools only parse,
map and render.

The completion lane is unchanged: a settled child announces itself through the
runner's ``notices`` (user + synthetic), so ``wait_agent`` deliberately returns a
status line rather than the child's prose — the transcript already gets it.
"""

from __future__ import annotations

import time
from typing import Any

from ai_coding.core.observers import EmitObserver
from ai_coding.core.scheduler import Scheduler
from ai_coding.domain.message import ToolResult
from ai_coding.domain.orchestration import TaskRequest
from ai_coding.domain.subagent import ParentContext, SubAgentPoolFull, SubagentTask
from ai_coding.tools.base import (
    BaseTool,
    ToolContext,
    parse_readonly,
    resolve_ctx,
)

__all__ = [
    "CloseAgentTool",
    "InterruptAgentTool",
    "ListAgentsTool",
    "ResumeAgentTool",
    "SendMessageTool",
    "SpawnAgentTool",
    "WaitAgentTool",
]

#: ``wait_agent`` bounds (C-5a): below a second a wait cannot observe anything,
#: above ten minutes it outlives the turn it serves.
_MIN_WAIT_MS = 1_000
_MAX_WAIT_MS = 600_000
_DEFAULT_WAIT_MS = 30_000

#: ``send_message`` keeps one message to what a model step can actually read:
#: anything longer belongs in ``task``/a file, not in a boundary injection.
_MAX_MESSAGE_CHARS = 2_000


class SpawnAgentTool(BaseTool):
    """Start a background sub-agent and hand back its run id immediately."""

    #: Spawning writes the background table and can start a writable child;
    #: the readonly/plan gate must refuse it (the old ``SubAgentTool`` kept the
    #: fail-closed ``mutates_world=True`` for the same reason).
    parallel_safe = False
    mutates_world = True
    #: P1b: ``readonly=true`` starts a child that cannot write, so the
    #: read-only surface keeps the tool and the gate judges each call.
    readonly_capable = True
    #: P2-C1: spawning hands work to the scheduler; the child owns its slot.
    occupies_slot = False

    def mutates_world_for(self, params: dict[str, Any] | None) -> bool:
        # Strict: only a literal ``true`` is a read-only child; anything else
        # (false, "false", absent) is a writable one (fail-closed).
        return parse_readonly(params) is not True

    def write_paths_for(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> tuple[str, ...] | None:
        # P2-B1: a child manages its own writes (and an isolated one writes in
        # its own worktree).  Spelled out so a future default change cannot
        # make this claim the parent's whole workspace.
        return None

    def __init__(
        self,
        runner: Any = None,
        worktree_manager: Any = None,
        isolate: bool = True,
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None and isinstance(runner, Scheduler):
            scheduler, runner = runner, None
        self._scheduler = (
            scheduler
            if scheduler is not None
            else (Scheduler(runner, worktree_manager=worktree_manager)
                  if runner is not None else None)
        )
        self._isolate = isolate

    @property
    def name(self) -> str:
        return "spawn_agent"

    def permission_name(self, params: dict[str, Any] | None = None) -> str:
        # The operator's "subagent" deny must cover starting a child, exactly
        # as it covered ``subAgent``.  A new name is not a new permission.
        return "subAgent"

    @property
    def description(self) -> str:
        return (
            "Start a sub-agent in the background and return its id at once; "
            "the parent turn keeps going. 'fork_turns' chooses what the child "
            "starts from: 'none' (default) gives it only the task text, 'all' "
            "gives it this conversation's messages and rules. Use 'name' for a "
            "short label and 'agent' for a subagent id (a primary agent cannot "
            "be spawned). Set 'readonly' to true to start a child that reads "
            "the live workspace and cannot write (no worktree). The child's "
            "completion arrives on its own as a notice; wait_agent observes "
            "one, list_agents shows the table and close_agent cancels one."
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
                "readonly": {
                    "type": "boolean",
                    "description": (
                        "Run the child read-only: it reads the live workspace "
                        "(no worktree) and cannot write. Defaults to false."
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Optional subagent id from the agent table. Defaults to "
                        "general. Primary agents cannot be spawned."
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
            # Codex rejects an unknown fork mode rather than silently choosing
            # one: the two modes differ in what the child can see and be held to.
            return ToolResult(
                success=False,
                output="error: 'fork_turns' must be one of none, all",
                error="invalid_fork_turns",
            )
        readonly = parse_readonly(params)
        if readonly is None:
            return ToolResult(
                success=False,
                output="error: 'readonly' must be a boolean",
                error="invalid_readonly",
            )
        raw_agent = params.get("agent")
        agent = (
            str(raw_agent).strip()
            if isinstance(raw_agent, str) and raw_agent.strip()
            else None
        )

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
        elif context.permissions:
            # C-5a review: the rule layer must travel even when the history
            # does not.  ``fork_turns="none"`` means "no parent messages", not
            # "ignore the operator's deny rules" -- dropping them would let a
            # clean-context child write what the parent was denied.
            parent = ParentContext(
                messages=(),
                permissions=tuple(context.permissions),
            )

        try:
            packet = SubagentTask.from_objective(
                task,
                context_mode=context_mode,
                agent=agent,
                readonly=readonly,
                isolate=self._isolate,
            )
            if self._scheduler is None:
                raise RuntimeError("no scheduler configured")
            request = TaskRequest.for_child(
                packet,
                name=name,
                origin_session=context.session_id,
                parent_span=context.call_id,
            )
            _request_id, run_id = await self._scheduler.spawn(
                request,
                parent=parent,
                observer=EmitObserver(context.emit),
                emit=context.emit,
            )
        except SubAgentPoolFull as exc:
            return ToolResult(
                success=False,
                output=f"agent not started: {exc}",
                error="agents_exhausted",
            )
        except RuntimeError as exc:
            # C-5a review: a failed start is not a saturated pool; the two need
            # different retry guidance (and the exception type says which).
            return ToolResult(
                success=False,
                output=f"agent not started: {exc}",
                error="agent_start_failed",
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
    occupies_slot = False

    def __init__(
        self,
        runner: Any = None,
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None and isinstance(runner, Scheduler):
            scheduler, runner = runner, None
        self._scheduler = (
            scheduler
            if scheduler is not None
            else (Scheduler(runner) if runner is not None else None)
        )

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
        if self._scheduler is None:
            return ToolResult(
                success=False, output="error: no scheduler configured",
                error="unknown_agent", data={"agent_id": agent_id},
            )
        resolved = _resolve_agent_id(self._scheduler, agent_id)
        status, elapsed_ms = await self._scheduler.wait(resolved, timeout_ms)
        output = f"agent {resolved}: {status} ({elapsed_ms} ms)"
        data = {"agent_id": resolved, "status": status, "elapsed_ms": elapsed_ms}
        if status == "unknown":
            return ToolResult(
                success=False, output=output, error="unknown_agent", data=data
            )
        return ToolResult(success=True, output=output, data=data)


class ListAgentsTool(BaseTool):
    """The background table: what is running and what has settled."""

    #: Reading the table has no side effects; sibling calls may all read it.
    parallel_safe = True
    mutates_world = False
    occupies_slot = False

    def __init__(
        self,
        runner: Any = None,
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None and isinstance(runner, Scheduler):
            scheduler, runner = runner, None
        self._scheduler = (
            scheduler
            if scheduler is not None
            else (Scheduler(runner) if runner is not None else None)
        )

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
        rows = (
            [
                view for view in self._scheduler.tasks().list()
                if view.kind == "background" and view.run_id
            ]
            if self._scheduler is not None
            else []
        )
        if not rows:
            return ToolResult(
                success=True,
                output="no background agents",
                data={"agents": []},
            )
        now = time.monotonic()
        lines = []
        for row in rows:
            display_id = row.run_id or row.id
            elapsed = row.elapsed(now)
            line = f"- {display_id} {row.name} [{row.status}] {elapsed:.0f}s"
            if row.changed_paths:
                line += f" · 改动 {len(row.changed_paths)} 个文件"
            if row.branch:
                line += f" · 分支 {row.branch}（未合并）"
            if row.commit_error:
                line += f" · 提交失败: {row.commit_error}"
            if row.degraded:
                line += f" · 降级: {row.degraded}"
            lines.append(line)
        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={"agents": [row.to_dict() for row in rows]},
        )


class CloseAgentTool(BaseTool):
    """Close one child (cancel its turn, release its loop, drop the handle)."""

    #: Cancelling a run changes the world (its work stops here).
    parallel_safe = False
    mutates_world = True
    occupies_slot = False

    def __init__(
        self,
        runner: Any = None,
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None and isinstance(runner, Scheduler):
            scheduler, runner = runner, None
        self._scheduler = (
            scheduler
            if scheduler is not None
            else (Scheduler(runner) if runner is not None else None)
        )

    @property
    def name(self) -> str:
        return "close_agent"

    def permission_name(self, params: dict[str, Any] | None = None) -> str:
        # Same permission as starting one: stopping a child is a subagent act.
        return "subAgent"

    @property
    def description(self) -> str:
        return (
            "Close a sub-agent by id: cancel its current turn and release it. "
            "The child's session, already-written commit/branch and ledger stay "
            "as they are, so resume_agent can reopen it later. A settled or "
            "unknown id changes nothing."
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
        if self._scheduler is None:
            return ToolResult(
                success=False, output="unknown agent: no scheduler configured",
                error="unknown_agent", data={"agent_id": agent_id, "closed": False},
            )
        resolved = _resolve_agent_id(self._scheduler, agent_id)
        ambiguous = _ambiguous_result(self._scheduler, agent_id)
        if ambiguous is not None:
            return ambiguous
        # C1: the registry replaces the runner's private table/handle probe.
        view = self._scheduler.tasks().get(agent_id)
        kind = self._scheduler.resolve_target(agent_id)[0]
        if view is None and resolved == agent_id and kind == "unknown":
            return ToolResult(
                success=False,
                output=f"unknown agent: {agent_id}",
                error="unknown_agent",
                data={"agent_id": resolved, "closed": False},
            )
        closed = bool(await self._scheduler.close(resolved))
        info = self._scheduler.close_info(resolved)
        commit = info.get("commit")
        commit_error = info.get("commitError")
        output = f"closed {resolved}" if closed else f"not running: {resolved}"
        data: dict[str, Any] = {"agent_id": resolved, "closed": closed}
        if commit:
            output += f" · 提交 {commit[:7]}"
            data["commit"] = commit
        elif commit_error:
            output += f" · 提交失败: {commit_error}"
            data["commit_error"] = commit_error
        return ToolResult(success=True, output=output, data=data)


class SendMessageTool(BaseTool):
    """Deliver a message to one child (running turn, or a new one if idle)."""

    #: Sending can start a writable child's turn; the plan/readonly gate hides it.
    parallel_safe = False
    mutates_world = True
    occupies_slot = False

    def __init__(
        self,
        runner: Any = None,
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None and isinstance(runner, Scheduler):
            scheduler, runner = runner, None
        self._scheduler = (
            scheduler
            if scheduler is not None
            else (Scheduler(runner) if runner is not None else None)
        )

    @property
    def name(self) -> str:
        return "send_message"

    def permission_name(self, params: dict[str, Any] | None = None) -> str:
        # Same permission as spawning one: talking to a child is a subagent act.
        return "subAgent"

    @property
    def description(self) -> str:
        return (
            "Send a message to a sub-agent by id or name. A running agent reads "
            "it at its next step; an idle (or interrupted) agent starts a new "
            "turn with it. Keep it under 2000 characters and put long material "
            "in a file the child can read. The reply comes back as a notice."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "The agent id (or unique name) to message.",
                },
                "message": {
                    "type": "string",
                    "description": "The message text (non-empty, max 2000 chars).",
                },
            },
            "required": ["target", "message"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        target = str(params.get("target", "") or "").strip()
        message = str(params.get("message", "") or "").strip()
        if not target:
            return ToolResult(
                success=False, output="error: 'target' is required",
                error="missing_target",
            )
        if not message or len(message) > _MAX_MESSAGE_CHARS:
            return ToolResult(
                success=False,
                output=(
                    "error: 'message' must be non-empty and at most "
                    f"{_MAX_MESSAGE_CHARS} characters"
                ),
                error="invalid_message",
            )
        if self._scheduler is None:
            return ToolResult(
                success=False, output="message not sent: no scheduler configured",
                error="unknown_agent", data={"target": target, "status": "unknown"},
            )
        resolved = _resolve_agent_id(self._scheduler, target)
        ambiguous = _ambiguous_result(self._scheduler, target)
        if ambiguous is not None:
            return ambiguous
        try:
            status, run_id = await self._scheduler.send(resolved, message)
        except SubAgentPoolFull as exc:
            return ToolResult(
                success=False, output=f"message not sent: {exc}",
                error="agents_exhausted",
            )
        data = {"target": run_id or resolved, "status": status}
        if status == "unknown":
            return ToolResult(
                success=False, output=f"unknown agent: {target}",
                error="unknown_agent", data=data,
            )
        if status == "queued":
            output = f"message queued to {run_id}"
        else:
            output = f"message started a turn on {run_id}"
        return ToolResult(success=True, output=output, data=data)


class InterruptAgentTool(BaseTool):
    """Cancel a child's current turn and keep the child addressable."""

    #: Cutting a running turn changes the world (its work stops here).
    parallel_safe = False
    mutates_world = True
    occupies_slot = False

    def __init__(
        self,
        runner: Any = None,
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None and isinstance(runner, Scheduler):
            scheduler, runner = runner, None
        self._scheduler = (
            scheduler
            if scheduler is not None
            else (Scheduler(runner) if runner is not None else None)
        )

    @property
    def name(self) -> str:
        return "interrupt_agent"

    def permission_name(self, params: dict[str, Any] | None = None) -> str:
        return "subAgent"

    @property
    def description(self) -> str:
        return (
            "Interrupt a sub-agent's current turn. The agent, its conversation "
            "and its workspace survive, so you can send_message it again or "
            "resume_agent it. An unknown or already-finished id still succeeds "
            "and reports what it saw. No completion notice is sent for an "
            "interrupt."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "The agent id (or unique name) to interrupt.",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        target = str(params.get("target", "") or "").strip()
        if not target:
            return ToolResult(
                success=False, output="error: 'target' is required",
                error="missing_target",
            )
        if self._scheduler is None:
            return ToolResult(
                success=False, output="error: no scheduler configured",
                error="unknown_agent", data={"target": target},
            )
        resolved = _resolve_agent_id(self._scheduler, target)
        ambiguous = _ambiguous_result(self._scheduler, target)
        if ambiguous is not None:
            return ambiguous
        previous, status = await self._scheduler.interrupt(resolved)
        previous_display = previous or "none"
        return ToolResult(
            success=True,
            output=f"agent {resolved}: {previous_display} -> {status}",
            data={
                "target": resolved,
                "previous_status": previous,
                "status": status,
            },
        )


class ResumeAgentTool(BaseTool):
    """Reopen a child that was interrupted or closed (or lost to a restart)."""

    #: Resuming rebuilds a writable child's loop; the gate must be able to hide it.
    parallel_safe = False
    mutates_world = True
    occupies_slot = False

    def __init__(
        self,
        runner: Any = None,
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        if scheduler is None and isinstance(runner, Scheduler):
            scheduler, runner = runner, None
        self._scheduler = (
            scheduler
            if scheduler is not None
            else (Scheduler(runner) if runner is not None else None)
        )

    @property
    def name(self) -> str:
        return "resume_agent"

    def permission_name(self, params: dict[str, Any] | None = None) -> str:
        return "subAgent"

    @property
    def description(self) -> str:
        return (
            "Reopen a sub-agent by id after an interrupt or a close, or after a "
            "restart (its session and metadata are on disk). Resume does not "
            "start a turn: send_message it to continue. An agent that is still "
            "running is a no-op."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The id (or unique name) to resume.",
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
                success=False, output="error: 'agent_id' is required",
                error="missing_agent_id",
            )
        if self._scheduler is None:
            return ToolResult(
                success=False, output="agent not found: no scheduler configured",
                error="not_found", data={"agent_id": agent_id, "status": "not_found"},
            )
        resolved = _resolve_agent_id(self._scheduler, agent_id)
        ambiguous = _ambiguous_result(self._scheduler, agent_id)
        if ambiguous is not None:
            return ambiguous
        status, run_id = await self._scheduler.resume(resolved)
        name = run_id or resolved
        data = {"agent_id": name, "status": status}
        if status == "not_found":
            return ToolResult(
                success=False, output=f"agent {name}: not found",
                error="not_found", data=data,
            )
        return ToolResult(success=True, output=f"agent {name}: {status}", data=data)


def _ambiguous_result(runner: Any, target: str) -> ToolResult | None:
    """A "which one did you mean?" refusal for a name that matches several.

    ``unknown_agent`` for a name ``list_agents`` just printed is the least
    useful answer: the model cannot tell a typo from a collision.
    """
    classify = getattr(runner, "resolve_target", None)
    if not callable(classify):
        return None
    kind, _run_id, candidates = classify(target)
    if kind != "ambiguous":
        return None
    return ToolResult(
        success=False,
        output=(
            f"ambiguous target '{target}': matches {', '.join(candidates)}; "
            "use one of those ids"
        ),
        error="ambiguous_target",
        data={"target": target, "candidates": list(candidates)},
    )


def _resolve_agent_id(runner: Any, agent_id: str) -> str:
    """A full run id for a short prefix, when the runner can resolve one."""
    resolve = getattr(runner, "resolve_run_id", None)
    if callable(resolve):
        return resolve(agent_id) or agent_id
    return agent_id


def _clamp_timeout(raw: Any) -> int:
    """The wait bound actually used: default 30s, clamped to 1s..10min."""
    try:
        value = int(raw) if raw is not None else _DEFAULT_WAIT_MS
    except (TypeError, ValueError):
        value = _DEFAULT_WAIT_MS
    return max(_MIN_WAIT_MS, min(_MAX_WAIT_MS, value))
