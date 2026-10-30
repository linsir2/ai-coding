"""Sub-agent orchestration (R1a-5).

A sub-agent is a self-contained run of the **same** loop the parent uses: it gets
the parent turn's request view and system prompt as its starting context, its own
task list, its own (sub-agent-free) tool stack, and — when the workspace is a git
repo and isolation is on — its own worktree.  Its reasoning is discarded; it
returns a structured packet which is also written to a ledger file.

Design notes (docs/r1a-5-analysis.md):
  - the semaphore is taken *before* the worktree, so the concurrency cap also
    bounds the number of worktrees in flight;
  - timeouts are a status, not an exception: they must be reportable to the
    parent so it can decide what to do next;
  - a sub-agent must not litter the user's ``sessions/`` directory.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import BackgroundTaskDone
from ai_coding.core.guard import build_guards
from ai_coding.core.observers import ScopedObserver
from ai_coding.core.steering import SteeringQueue
from ai_coding.core.turn_report import report_degraded
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.agents import AgentCatalog, AgentSpec
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.subagent import (
    BackgroundTask,
    ParentContext,
    SubagentResult,
    extract_json_facts,
)
from ai_coding.infra.deadline import DeadlineExpired, run_with_deadline
from ai_coding.infra.json_utils import to_json_pretty
from ai_coding.infra.time_utils import now_utc
from ai_coding.skills.registry import SkillRegistry
from ai_coding.tools.registry import ToolRegistry

EngineFactory = Callable[[], ChatEngine]
BuildTools = Callable[[str | None], ToolRegistry]

_log = logging.getLogger(__name__)

# TurnStatus value -> packet status
_STATUS_MAP: dict[str, str] = {
    "completed": "completed",
    "max_iterations": "max_iterations",
    "timeout": "timeout",
    # R4b: running out of token budget is partial work, like running out of
    # iterations — not a failure.  Omitting it would read as "error".
    "budget": "max_iterations",
    "stopped": "error",
    "aborted": "error",
    "error": "error",
}

# Fraction of the sub-agent's budget the *inner* turn deadline may use; the rest
# is left for finalisation and for the outer backstop (R2a).
_INNER_BUDGET_RATIO = 0.9

#: Completion summary tail for the announce event (R1-2): the full packet
#: lives in the ledger file; the event carries a pointer, not the prose.
_SUMMARY_TAIL = 200

#: Background table cap (R1-2): a long-lived process must not grow the table
#: without bound.  Running rows are never pruned; the oldest terminal rows go
#: first.  Restarts lose the whole table anyway (documented, Hermes rule).
_MAX_BACKGROUND_ROWS = 200


class _NullSessions:
    """Swallows saves: a sub-agent's transcript is its ledger, not a session file."""

    def save(self, session: SessionData) -> None:
        return None

    def mark_pending_approval(
        self, session_id: str, request: dict[str, Any] | None
    ) -> None:
        """P3: a sub-agent has no session file to mark, and nobody to ask
        anyway -- its approval callback is the auto-approver."""
        return None

    def mark_pending_posture(
        self, session_id: str, payload: dict[str, Any] | None
    ) -> None:
        """M1b T10: a sub-agent owns no session file and takes no operator
        flip — the same no-op as ``save``, and the same reason."""
        return None


class SubAgentRunner:
    """Run one sub-agent task, optionally inside a git worktree sandbox."""

    def __init__(
        self,
        app_config: Any,
        engine_factory: EngineFactory,
        build_tools: BuildTools,
        *,
        ledger_root: str | Path | None = None,
        max_concurrent: int = 3,
        timeout_seconds: float = 600.0,
        settings: AIConfig | None = None,
        approval: Any | None = None,
        workspace: str = ".",
        observer: Any | None = None,
        base_instructions: str = "",
        skills: SkillRegistry | None = None,
        notices: SteeringQueue | None = None,
        #: M1b: the agent table a ``subAgent`` target is resolved against.
        #: ``None`` keeps the pre-M1b behaviour: no target, no agent layer.
        agents: AgentCatalog | None = None,
        #: r25/v2: the project-instructions loader a ``full`` child uses to build
        #: its own system prompt (the parent's assembled prompt is no longer
        #: inherited).  ``None`` keeps tests and bare callers free of filesystem
        #: reads; production passes ``ProjectInstructionsLoader(workspace)``.
        project: Any | None = None,
    ) -> None:
        self._app_config = app_config
        self._skills = skills
        # M2 fix: the guard chain used to be assembled only for the parent loop,
        # so every sub-agent got ``AgentLoop(guards=None)`` -- which the
        # dispatcher turns into an empty chain, i.e. no red lines, no path
        # strictness, no duplicate-call budget.  A worktree isolates *files*; it
        # never relaxed the policy, and a child inherits the parent's policy
        # rather than a copy of the parent's turn counters.
        self._guards = build_guards(app_config)
        self._engine_factory = engine_factory
        self._build_tools = build_tools
        self._ledger_root = Path(ledger_root) if ledger_root is not None else None
        self._timeout = float(timeout_seconds)
        self._settings = settings or AIConfig()
        # No human is present inside a sub-agent to answer a WARN prompt; the DENY
        # tier still blocks dangerous calls regardless of the callback.
        from ai_coding.security.approval import AutoApproveCallback

        self._approval = approval if approval is not None else AutoApproveCallback()
        self._workspace = workspace
        # r25/v2: the child's own project section (full mode only).  Read from the
        # main workspace, not the worktree: both see the same project file.
        self._project = project
        # R4a: the parent's observer, so a sub-agent stops being a black box.  Its
        # events are wrapped with their scope/parent by ``ScopedObserver`` below.
        self._observer = observer
        # R7e-4: what a ``task_only`` child gets instead of the parent's prompt.
        self._base_instructions = str(base_instructions or "")
        # P0-2: where a completion goes so the *model* can read it.  The event
        # already reaches the operator; the tool result points at ``/tasks``,
        # which is a human command, so without this lane a background run is a
        # one-way door for the model that started it.
        self._notices = notices
        self._agents = agents
        self._max_concurrent_n = max(1, int(max_concurrent))
        self._semaphore = asyncio.Semaphore(self._max_concurrent_n)
        # R1-2: background runs tracked across turns: run id -> row, plus the
        # live asyncio tasks (strong refs so they survive the spawning turn),
        # plus the announce context (emit/origin/name) keyed the same way.
        self._background: dict[str, BackgroundTask] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._announce: dict[str, tuple[Any, str, str]] = {}

    @property
    def timeout_seconds(self) -> float:
        """The budget the tool layer advertises as this runner's per-call ceiling."""
        return self._timeout

    @property
    def context_mode(self) -> str:
        """``full`` (the parent's view) or ``task_only`` (a clean window), R7e-4."""
        return str(getattr(self._settings, "subagent_context_mode", "full") or "full")

    @property
    def max_concurrent(self) -> int:
        """How many children may run at once (B0: a fan-out's wall clock needs it)."""
        return self._max_concurrent_n

    async def run(
        self,
        task: str,
        name: str = "sub-agent",
        *,
        worktree_manager: Any = None,
        isolate: bool = True,
        parent: ParentContext | None = None,
        parent_span: str = "",
        observer: Any | None = None,
        output_schema: dict[str, Any] | None = None,
        agent: str | None = None,
        context_mode: str | None = None,
        readonly: bool = False,
    ) -> SubagentResult:
        """Run ``task`` and return its packet.  Never raises for a sub-agent failure.

        ``observer`` is a per-call override for the constructor's observer:
        the tool layer fans the parent's emit through here, so child events
        reach the parent trace (previously the slot was never filled and the
        child ran as a black box — measured: zero scoped events over 23 runs).

        ``output_schema`` (R1-3) is a JSON-shape contract for the conclusion:
        the child is instructed to return its verdict as JSON, and the packet
        carries the parsed ``facts`` plus a ``schema_valid`` verdict.  The raw
        conclusion always survives — a bad brace must not kill an hour of work.

        ``context_mode`` (B0) overrides the configured window for this call.
        A fan-out of N research children must not copy the parent history N
        times; the caller passes ``task_only`` and puts every needed fact in the
        brief.  Unknown values fall back to the configured mode rather than
        silently changing what a child sees.

        ``readonly`` (B0) is the hard read-only switch: the child's session is
        readonly, so the tool surface drops mutating tools and the dispatch gate
        refuses them.  A fan-out does not rely on the ``explore`` agent id alone
        — a project file may override that id whole.
        """
        active = self._observer if observer is None else observer
        mode = self._resolve_context_mode(self.context_mode, context_mode)
        async with self._semaphore:
            return await self._run_isolated(
                task, name, parent, parent_span, active,
                worktree_manager, isolate, output_schema, agent, mode, readonly,
            )

    @staticmethod
    def _resolve_context_mode(configured: str, requested: str | None) -> str:
        """The mode a child runs with: a valid override, else the configured one.

        Anything other than the two documented windows is ignored on purpose:
        a typo must not become a third, undefined context semantics.
        """
        mode = (requested or configured or "full").strip().lower()
        if mode not in ("full", "task_only"):
            mode = (configured or "full").strip().lower()
        return mode if mode in ("full", "task_only") else "full"

    async def _run_isolated(
        self,
        task: str,
        name: str,
        parent: ParentContext | None,
        parent_span: str,
        observer: Any | None,
        worktree_manager: Any | None,
        isolate: bool,
        output_schema: dict[str, Any] | None = None,
        agent: str | None = None,
        context_mode: str = "full",
        readonly: bool = False,
    ) -> SubagentResult:
        """Worktree branch shared by the synchronous and background paths."""
        if (
            isolate
            and worktree_manager is not None
            and await worktree_manager.can_isolate()
        ):
            try:
                async with worktree_manager.temporary(name) as wt:
                    result = await self._run_in(
                        task, name, str(wt.path), parent,
                        parent_span=parent_span, observer=observer,
                        output_schema=output_schema, agent=agent,
                        context_mode=context_mode, readonly=readonly,
                    )
                    # r25/v2: the worktree is force-removed on exit, so a write
                    # that is not committed dies with it.  Commit first, then
                    # hand the branch + sha to the parent; the directory goes,
                    # the branch stays.  A failed commit is reported as "no
                    # commit" (never swallowed into a fake success).
                    commit = await worktree_manager.commit_if_dirty(
                        str(wt.path), f"subagent: {name}"
                    )
                    if commit:
                        enriched = replace(
                            result, worktree_branch=wt.branch, commit=commit
                        )
                        # The ledger was written inside ``_packet`` before the
                        # commit existed; refresh it so the "full packet" file
                        # names the branch instead of a removed worktree.
                        _refresh_ledger(enriched)
                        return enriched
                    return result
            except WorktreeError:
                # Scoped like the child's own events: this record says the child
                # ran in the *main* workspace, and `report_degraded` carries no
                # name, so without the wrapper nothing ties it to a child — and
                # up to three run at once.
                report_degraded(
                    self._scoped_observer(name, parent_span, observer),
                    "worktree_isolate",
                    "the worktree could not be created",
                    "ran in the main workspace instead",
                )
        return await self._run_in(
            task, name, None, parent, parent_span=parent_span, observer=observer,
            output_schema=output_schema, agent=agent,
            context_mode=context_mode, readonly=readonly,
        )

    # ---- background runs (R1-2) -------------------------------------------

    def spawn_background(
        self,
        task: str,
        name: str = "sub-agent",
        *,
        worktree_manager: Any = None,
        isolate: bool = True,
        parent: ParentContext | None = None,
        parent_span: str = "",
        observer: Any | None = None,
        emit: Any = None,
        origin_session: str = "",
        output_schema: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> str:
        """Start a child without waiting; return its run id at once.

        The spawning turn pays nothing past this call — the child runs on
        under the same semaphore, timeout and ledger discipline as a
        synchronous run, and its completion lands as a ``BackgroundTaskDone``
        event on ``emit`` plus a row in :meth:`task_list`.

        A saturated pool rejects loudly (``RuntimeError``) instead of
        queuing silently: the turn must not pay for work that never starts.
        (Check-then-act races degrade to background queuing, never to
        turn-blocking — the semaphore is still the true bound.)  Must be
        called from async context: the supervised task is created on the
        running loop.
        """
        if self._semaphore.locked():
            raise RuntimeError(
                "sub-agent concurrency exhausted: "
                f"{self._max_concurrent()} background slots busy; "
                "retry later or raise ai.max_concurrent_subagents"
            )
        run_id = uuid.uuid4().hex[:12]
        active = self._observer if observer is None else observer
        self._background[run_id] = BackgroundTask(
            run_id=run_id, name=name, task=task, status="running",
            started=time.monotonic(),
        )
        self._prune_background()
        background_task = asyncio.get_running_loop().create_task(
            self._supervised(
                run_id, task=task, name=name, worktree_manager=worktree_manager,
                isolate=isolate, parent=parent, parent_span=parent_span,
                observer=active, emit=emit, origin_session=origin_session,
                output_schema=output_schema, agent=agent,
            ),
            name=f"subagent:{run_id}",
        )
        self._tasks[run_id] = background_task
        # A task cancelled before its first step never runs its body — the
        # supervised path below would never settle it.  The reaper closes
        # that hole: whatever state the row is still in, it gets settled.
        background_task.add_done_callback(functools.partial(self._reap, run_id))
        # The announce context rides alongside (the frozen row cannot hold it).
        self._announce[run_id] = (emit, origin_session, name)
        return run_id

    def cancel(self, run_id: str) -> bool:
        """Stop a running background task.  False when unknown or settled."""
        row = self._background.get(run_id)
        background_task = self._tasks.get(run_id)
        if row is None or background_task is None or row.status != "running":
            return False
        background_task.cancel()
        return True

    def resolve_run_id(self, prefix: str) -> str | None:
        """The full run id a printed prefix stands for, or None.

        ``/tasks`` shows ``run_id[:12]`` and ``cancel`` needs the whole thing;
        without this bridge the operator can only copy something that does not
        work.  Ambiguous prefixes resolve to None (the caller lists candidates)
        — the same rule ``/resume`` follows via ``SessionService.resolve``.
        """
        if not prefix:
            return None
        exact = self._background.get(prefix)
        if exact is not None:
            return prefix
        matches = [key for key in self._background if key.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def task_list(self) -> list[BackgroundTask]:
        """Background rows, oldest first (running and settled alike)."""
        return sorted(self._background.values(), key=lambda row: row.started)

    def _max_concurrent(self) -> int:
        return self._max_concurrent_n

    def _prune_background(self) -> None:
        """Bound the table: oldest terminal rows go first, running never."""
        terminal = sorted(
            (row for row in self._background.values() if row.status != "running"),
            key=lambda row: row.started,
        )
        while len(self._background) > _MAX_BACKGROUND_ROWS and terminal:
            self._background.pop(terminal.pop(0).run_id, None)

    async def _supervised(
        self,
        run_id: str,
        *,
        task: str,
        name: str,
        worktree_manager: Any | None,
        isolate: bool,
        parent: ParentContext | None,
        parent_span: str,
        observer: Any | None,
        emit: Any,
        origin_session: str,
        output_schema: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> None:
        """Run one background child to settlement, then announce it.

        Never raises: an unretrieved task exception is noise at shutdown, so
        every failure mode below ends in a table row plus (best-effort) event.
        """
        try:
            async with self._semaphore:
                result = await self._run_isolated(
                    task, name, parent, parent_span, observer,
                    worktree_manager, isolate, output_schema, agent,
                    self.context_mode, False,
                )
        except asyncio.CancelledError:
            result = SubagentResult(
                status="cancelled", error="cancelled by user",
                truncated=True, exit_reason="cancelled",
            )
        except Exception as exc:  # noqa: BLE001 — _run_in promises never-raise; belt.
            result = SubagentResult(
                status="error", error=f"supervisor: {exc}",
                truncated=True, exit_reason="supervisor_error",
            )
        self._finish_background(run_id, result, emit, origin_session)

    def _finish_background(
        self, run_id: str, result: SubagentResult, emit: Any, origin_session: str
    ) -> None:
        """Settle the row, drop the task ref, announce best-effort."""
        row = self._background.get(run_id)
        self._tasks.pop(run_id, None)
        self._announce.pop(run_id, None)
        if row is None or row.status != "running":
            return
        self._background[run_id] = replace(
            row, status=result.status, ended=time.monotonic(),
            error=result.error, usage=result.usage,
            # C-3b: the pointer subagent_list hands out once the notice may be
            # gone (the ledger file itself was written by ``_with_ledger``).
            ledger_path=result.ledger_path,
        )
        if emit is None:
            return
        summary = (result.conclusion or result.error or "").strip().replace("\n", " ")
        # r25/v2/F5: a background schema call used to lose its verdict here --
        # the model saw prose and a ledger pointer only.  One short marker is
        # enough; the facts stay in the ledger.
        if len(summary) > _SUMMARY_TAIL:
            summary = summary[:_SUMMARY_TAIL] + "…"
        # Append after the cut: a long conclusion must not eat the verdict.
        if result.schema_valid is False:
            summary = f"{summary}（结构校验未过）".strip()
        self._announce_to_model(
            run_id, row.name, result.status, summary, result.ledger_path,
            result.worktree_branch,
        )
        try:
            emit(BackgroundTaskDone(
                run_id=run_id, name=row.name, status=result.status,
                summary=summary, truncated=result.truncated,
                origin_session=origin_session,
                # R1-3: the pointer the event docstring always promised — the
                # full packet (facts included) lives in this file.  Without
                # it a background+schema run's facts would be unreachable.
                ledger_path=result.ledger_path,
            ))
        except Exception:  # noqa: BLE001 — observation must not break settlement
            _log.debug("background announce failed for %s", run_id, exc_info=True)

    def _announce_to_model(
        self,
        run_id: str,
        name: str,
        status: str,
        summary: str,
        ledger_path: str | None,
        branch: str | None = None,
    ) -> None:
        """Hand the settlement to the parent's loop (P0-2).

        Separate from the event on purpose: the event is an observation the
        operator may miss, and a failed ``put`` must never disturb a settlement
        that has already been recorded on the row.
        """
        if self._notices is None:
            return
        try:
            self._notices.put(
                _notice_text(run_id, name, status, summary, ledger_path, branch)
            )
        except Exception:  # noqa: BLE001 — same reason as the event above
            _log.debug("background notice failed for %s", run_id, exc_info=True)

    def _reap(self, run_id: str, _task: asyncio.Task[None] | None = None) -> None:
        """Settle a row whose task died without going through ``_finish``.

        A task cancelled before its first step never runs the supervised body
        at all — without this, ``cancel()`` right after spawn would leave the
        row ``running`` forever.  Runs exactly once per task (done-callback);
        a no-op unless the row is still live.
        """
        row = self._background.get(run_id)
        self._tasks.pop(run_id, None)
        announce = self._announce.pop(run_id, None)
        if row is None or row.status != "running":
            return
        self._background[run_id] = replace(
            row, status="cancelled", ended=time.monotonic(),
            error="cancelled before start",
        )
        if announce is None:
            return
        emit, origin_session, name = announce
        if emit is None:
            return
        self._announce_to_model(
            run_id, name, "cancelled", "cancelled before start", None
        )
        try:
            emit(BackgroundTaskDone(
                run_id=run_id, name=name, status="cancelled",
                summary="cancelled before start", truncated=True,
                origin_session=origin_session,
            ))
        except Exception:  # noqa: BLE001 — observation must not break settlement
            _log.debug("background reap announce failed for %s", run_id, exc_info=True)

    # ---- internals --------------------------------------------------------

    def _scoped_observer(self, name: str, parent_span: str, inner: Any) -> Any:
        """Wrap an observer so the child's events carry their parentage."""
        if inner is None:
            return None
        return ScopedObserver(
            scope=f"subagent:{name}:{parent_span or 'root'}",
            parent=parent_span or "subagent",
            inner=inner,
        )

    def _child_gate(self) -> Any:
        """The operator's own policy, for a child that cannot prompt (E-5, P0-1).

        Built by the same factory as the parent's gate
        (``PermissionGate.from_config``) and for the same reason the guard chain
        above is rebuilt here: a child inherits the *policy*, never a copy of the
        parent's mutable counters.  The tier that buys is the one that can still
        bite without a human -- ``permissions.deny`` is a hard DENY checked
        before grants (``PermissionGate.check`` 1.5) -- because every WARN tier
        degrades to "yes" here (``AutoApproveCallback``).  A gate carrying only
        the skill rule therefore turned the operator's config refusal into an
        execution, which is the whole difference this method exists to close.
        """
        from ai_coding.security.permission_gate import PermissionGate

        skills = self._skills

        def resolve(name: str) -> str | None:
            skill = skills.resolve(name) if skills is not None else None
            return skill.id if skill is not None else None

        # The main workspace, not the worktree: grants are keyed by the path the
        # operator was asked about, and the child has the same authority there.
        return PermissionGate.from_config(
            self._app_config, workspace=self._workspace, skill_resolver=resolve
        )

    def _resolve_target(self, agent: str | None) -> tuple[AgentSpec | None, str]:
        """Which agent the child speaks through — ``(spec, problem)``.

        Resolved here rather than in the tool because the table lives here:
        the tool only forwards what the model asked for (``None`` = not
        specified), so "the default" and "a target the model chose" stay
        distinguishable — which is what lets a loop with no table keep its
        pre-M1b behaviour.

        A ``None`` spec with no problem is the pre-M1b path: no agent layer.
        """
        if self._agents is None:
            if agent is None:
                return None, ""
            return None, "agent table not available"
        requested = agent if agent is not None else "general"
        spec = self._agents.get(requested)
        if spec is None:
            available = ", ".join(item.id for item in self._agents.subagents())
            return None, f"unknown agent: {requested} (available: {available or 'none'})"
        if not spec.is_subagent:
            return None, f"primary agents cannot be spawned: {requested}"
        return spec, ""

    async def _run_in(
        self,
        task: str,
        name: str,
        workdir: str | None,
        parent: ParentContext | None,
        *,
        parent_span: str = "",
        observer: Any | None = None,
        output_schema: dict[str, Any] | None = None,
        agent: str | None = None,
        context_mode: str = "full",
        readonly: bool = False,
    ) -> SubagentResult:
        target, problem = self._resolve_target(agent)
        if problem:
            return SubagentResult(status="error", error=problem)
        registry = self._build_tools(workdir)
        engine = self._engine_factory()
        ts = now_utc()
        # R7e-4 + r25/v2: which window this child gets.  ``task_only`` is a clean
        # context - the parent has to put every fact it needs into the task text.
        # Both modes build the child's **own** system prompt: inheriting the
        # parent's *assembled* prompt made the child append project/skills/methods
        # a second time (measured 617 -> 1140 chars) and advertised tools the
        # child does not have.  ``full`` differs only in the seeded history and
        # the project section.
        mode = context_mode
        prompt = (
            PromptAssembler(self._base_instructions)
            if self._base_instructions
            else None
        )
        if mode == "task_only":
            seeded: list[ChatMessage] = []
        else:
            seeded = list(parent.messages) if parent is not None else []
        session = SessionData(
            session_id=f"sub-{uuid.uuid4().hex[:12]}",
            title=name,
            created_time=ts,
            last_access_time=ts,
            messages=seeded,
            agent=target.id if target is not None else "build",
            # B0: the hard read-only switch.  A fan-out passes True so the
            # child's tool surface drops mutating tools; the agent id alone is
            # not a boundary (a project may override ``explore`` whole).
            readonly=readonly,
        )
        loop = AgentLoop(
            engine,
            _NullSessions(),  # type: ignore[arg-type]
            registry,
            approval=self._approval,
            gate=self._child_gate(),
            prompt=prompt,
            # The same registry the parent uses, so the child reads live skills
            # rather than the frozen copy in the parent's instructions. It used
            # to take none at all, which left the child holding the `skill` tool
            # (the tool registry always got the path) with no catalog naming
            # anything it could load.
            skills=self._skills,
            # r25/v2: the project section is the child's own; full mode only
            # (task_only keeps the R7e-4 clean window).
            project=self._project if mode != "task_only" else None,
            settings=self._subagent_settings(),
            guards=self._guards,
            workspace=workdir or self._workspace,
            observer=self._scoped_observer(name, parent_span, observer),
            # M1a ④: the parent's session layer, carried on the packet.  The
            # child's own session starts empty, so this is the only path by
            # which "deny edit *.env" reaches the work it delegates.
            session_permissions=parent.permissions if parent is not None else (),
            agents=self._agents,
        )
        try:
            # The inner loop has its own (slightly tighter) turn deadline, so it
            # normally finalises itself and reports a status.  This outer call is
            # a backstop for a loop that is stuck inside something that ignores
            # cancellation — ``asyncio.wait_for`` could not bound that.
            # R1-3: the format contract rides as a trailer on the task text —
            # the task *is* the child's user message, so no loop/engine change
            # is needed for the child to see it.
            child_task = task
            if output_schema:
                # One format instruction, never two.  The old prose
                # "uncertainty protocol" is gone: nothing read its notes and
                # the confidence it collected had no consumer.
                child_task += _format_instruction(output_schema)
            await run_with_deadline(
                loop.process_input(session, child_task),
                timeout=self._timeout,
                label="subagent_turn",
            )
        except DeadlineExpired:
            return SubagentResult(
                status="timeout",
                error=f"exceeded {self._timeout:g}s",
                workdir=workdir,
                # R0-3: an outer cut is still a cut — unfinished, with a cause.
                truncated=True,
                exit_reason="outer_timeout",
            )
        finally:
            try:
                await loop.aclose()
            except Exception:  # noqa: BLE001 — releasing resources must not mask the result
                pass
        return self._packet(loop, session, workdir, mode, output_schema)

    def _subagent_settings(self) -> AIConfig:
        """Settings for the sub-turn: its own wall-clock ceiling (R2a)."""
        configured = float(getattr(self._settings, "turn_timeout_seconds", 0.0) or 0.0)
        budget = self._timeout * _INNER_BUDGET_RATIO
        if configured > 0:
            budget = min(budget, configured)
        # C3 off: the parent is blocked on this child, so a continuation buys
        # the parent nothing but latency — and it multiplies the child's
        # iterations by (1 + max_auto_continuations).  Measured with an open
        # todo item and max_tool_iterations=3: 12 model calls instead of 4.
        return self._settings.model_copy(
            update={
                "turn_timeout_seconds": max(budget, 0.01),
                "max_auto_continuations": 0,
            }
        )

    def _packet(
        self, loop: AgentLoop, session: SessionData, workdir: str | None, mode: str = "full",
        output_schema: dict[str, Any] | None = None,
    ) -> SubagentResult:
        outcome = loop.last_outcome
        status = _STATUS_MAP.get(outcome.status.value if outcome else "error", "error")
        limit = int(getattr(self._settings, "max_subagent_conclusion_chars", 4000))
        # R6a: a sub-agent that ran out of budget still has something to say — the
        # harness handoff — and the parent should not have to guess why it stopped.
        conclusion = _truncate(
            (outcome.text or outcome.handoff) if outcome else None, limit
        )
        todos = (
            tuple(item.to_dict() for item in session.task.items)
            if session.task is not None
            else ()
        )
        is_error = status in {"error", "timeout"}
        truncated = status != "completed"
        exit_reason = None
        if outcome is not None and status != "completed":
            exit_reason = outcome.status.value
            if outcome.reason:
                exit_reason = f"{exit_reason}: {outcome.reason}"
        # R1-3: parse what the parent will actually read — the truncated
        # conclusion — so facts can never describe text the parent never saw.
        facts: dict[str, Any] | None = None
        schema_valid: bool | None = None
        schema_errors: tuple[str, ...] = ()
        if output_schema:
            facts, schema_valid, schema_errors = extract_json_facts(
                conclusion, output_schema
            )
        # F4's prose notes/confidence are gone: nothing read them (confidence
        # had no consumer at all; notes only wrote an unread trace file).
        result = SubagentResult(
            status=status,
            conclusion=conclusion,
            todos=todos,
            artifacts=tuple(loop.last_artifacts),
            iterations=outcome.iterations if outcome else 0,
            workdir=workdir,
            error=(outcome.reason if outcome and outcome.reason else None) if is_error else None,
            context_mode=mode,
            truncated=truncated,
            exit_reason=exit_reason,
            usage=dict(outcome.usage) if outcome is not None and outcome.usage else None,
            facts=facts,
            schema_valid=schema_valid,
            schema_errors=schema_errors,
        )
        return _with_ledger(result, self._ledger_root)


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _with_ledger(result: SubagentResult, root: Path | None) -> SubagentResult:
    """Write the full packet (untruncated) and return it with the path attached."""
    if root is None:
        return result
    payload = result.to_dict()
    try:
        target_dir = root / "transcripts" / "subagents"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{uuid.uuid4().hex[:12]}.json"
        path.write_text(to_json_pretty(payload), encoding="utf-8")
    except OSError:
        return result  # best-effort: no ledger, but the packet still flows
    return SubagentResult(
        status=result.status,
        conclusion=result.conclusion,
        todos=result.todos,
        artifacts=result.artifacts,
        iterations=result.iterations,
        workdir=result.workdir,
        ledger_path=str(path),
        error=result.error,
        context_mode=result.context_mode,
        truncated=result.truncated,
        exit_reason=result.exit_reason,
        usage=result.usage,
        facts=result.facts,
        schema_valid=result.schema_valid,
        schema_errors=result.schema_errors,
        worktree_branch=result.worktree_branch,
        commit=result.commit,
    )


def _refresh_ledger(result: SubagentResult) -> None:
    """Rewrite the ledger after branch/commit enrichment (r25/v2).

    The ledger is written inside ``_packet``, before the commit exists; without
    this rewrite the "full packet" file would name a worktree that is about to
    be deleted and carry no branch to find the work on.
    """
    if not result.ledger_path:
        return
    try:
        Path(result.ledger_path).write_text(
            to_json_pretty(result.to_dict()), encoding="utf-8"
        )
    except OSError:
        pass  # best-effort: the packet itself already carries the branch


def _format_instruction(schema: dict[str, Any]) -> str:
    """The machine contract trailer appended to the child's task text (R1-3).

    The child never sees the schema object, so the trailer has to carry enough
    of it: names **and** shapes.  A required key list alone is what let a real
    run return ``findings`` as an object while the schema said ``string[]``.
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = schema.get("required", []) if isinstance(schema, dict) else []
    required_keys = list(required) if isinstance(required, list) else []
    shapes = properties if isinstance(properties, dict) else {}
    required_desc = ", ".join(
        f"{key}: {_shape(shapes.get(key))}" for key in required_keys
    ) or ", ".join(map(str, required_keys))
    optional = [str(key) for key in shapes if key not in required_keys]
    lines = [
        "",
        "",
        "[response format — machine contract, not prose]",
        "Return your conclusion as a JSON object"
        + (f" with these required keys: {required_desc}." if required_desc else "."),
        "Output the JSON object and nothing else (no code fences, no commentary).",
        "If you cannot produce valid JSON, still write your conclusion as plain "
        "text — partial work is never discarded.",
    ]
    if optional:
        # The child never sees the schema object, so optional properties must be
        # named with their shape or they will never be filled correctly.
        described = ", ".join(f"{key}: {_shape(shapes.get(key))}" for key in optional)
        lines.insert(
            -1, f"Optional keys, when you can fill them: {described}."
        )
    return "\n".join(lines)


def _shape(spec: Any) -> str:
    """A compact type hint for one property (``string[]``, ``object``, …)."""
    if not isinstance(spec, dict):
        return "any"
    kind = spec.get("type")
    if kind == "array":
        items = spec.get("items")
        return f"{(items.get('type') if isinstance(items, dict) else None) or 'any'}[]"
    return str(kind or "any")


__all__ = ["SubAgentRunner"]

#: The four settlements ``BackgroundTaskDone`` can carry, in the operator's
#: language rather than the event's enum value.
_NOTICE_STATUS = {
    "completed": "完成",
    "timeout": "超时",
    "error": "失败",
    "cancelled": "已取消",
}


def _notice_text(
    run_id: str,
    name: str,
    status: str,
    summary: str,
    ledger_path: str | None,
    branch: str | None = None,
) -> str:
    """One harness line for a background run that settled.

    It is written for the model's next request, and it must survive
    ``SteeringQueue.put``'s two rules: never empty, never starting with ``/``
    (a command is the caller's to place).  A fixed marker prefix buys both, and
    the ledger pointer is what makes a 200-character summary enough -- the file
    is inside the workspace, so the model can read the whole packet.

    ``branch`` names where an isolated child committed its changes; without it
    a background write handoff has no address at all (the worktree is removed).
    """
    word = _NOTICE_STATUS.get(status, status)
    line = f"[子任务{word}] {name}（{run_id[:12]}）"
    body = " ".join(str(summary or "").split())
    if body:
        line += f"：{body}"
    if branch:
        line += f" · 分支 {branch}（未合并）"
    if ledger_path:
        line += f" · 完整回包 {ledger_path}"
    return line
