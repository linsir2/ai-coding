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
import json
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import BackgroundTaskDone
from ai_coding.core.execution_slots import (
    ExecutionSlotPool,
    Slot,
    enter_child,
    holding_slot,
)
from ai_coding.core.guard import build_guards
from ai_coding.core.notice_store import NoticeStore, clip_notice
from ai_coding.core.observers import ScopedObserver
from ai_coding.core.steering import SteeringQueue
from ai_coding.core.turn_report import report_degraded
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.agents import AgentCatalog, AgentSpec
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.subagent import (
    SUBAGENT_PACKET_VERSION,
    AgentPacket,
    BackgroundTask,
    PacketHeader,
    ParentContext,
    SubAgentPoolFull,
    SubagentResult,
    SubagentTask,
    extract_json_facts,
)
from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc
from ai_coding.security.permissions import rule_to_dict, rules_from_dicts
from ai_coding.service.session_service import SessionService
from ai_coding.skills.registry import SkillRegistry
from ai_coding.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from ai_coding.security.permissions import Rule

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

#: C-5a: the hard ceiling ``wait`` applies (the tool clamps to the same value).
_MAX_WAIT_SECONDS = 600.0


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


class _StartError(RuntimeError):
    """The child could not be opened (bad target, or no run id to address it)."""


@dataclass
class AgentHandle:
    """One addressable child (C-5b-1).

    ``_handles`` is the source of truth for addressing; ``_background`` is only
    a projection for ``/tasks``/``list_agents``.  A handle owns the child's
    ``SessionData`` and its ``AgentLoop``, so later phases can send another turn
    into the same conversation instead of rebuilding it.
    """

    run_id: str
    name: str
    task: str
    session: SessionData
    session_store: SessionService
    loop: AgentLoop | None
    workdir: str | None
    readonly: bool
    agent: str | None
    context_mode: str
    parent_permissions: tuple[Rule, ...]
    parent_span: str
    emit: Any
    origin_session: str
    output_schema: dict[str, Any] | None
    #: C-5b: the raw observer, so a rebuilt loop can re-scope it to this child.
    observer: Any = None
    #: C-5b: the persistent worktree a resumed turn keeps using (None = main).
    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_manager: Any = None
    #: C-5b: the child's tool registry.  Reused across a resume so the
    #: ``FileStateTracker`` inside ``write``/``edit`` keeps what the model has
    #: already read this session (a rebuilt registry answers "never read").
    registry: Any = None
    #: P2-A: the parent → child packet that started this run.  Kept so the
    #: ledger/meta can name the task and so a follow-up turn keeps the same
    #: context window/agent/read-only switches.
    request: SubagentTask | None = None
    #: P2-A: the settlement's write facts (per turn, then close).  They are
    #: projections of the latest result, not a second source of truth.
    commit: str | None = None
    commit_error: str | None = None
    #: P2-C3: the commit the worktree was created from.  Additive; old meta
    #: reads back as ``None`` and the merge queue falls back to merge-base.
    base_commit: str | None = None
    degraded: str | None = None
    #: P2-C2: the scheduler task this handle belongs to, its current fence, and
    #: the terminal latch ``stop_run`` sets.  A stopped handle never starts
    #: another turn, sends, or resumes.
    task_id: str = ""
    fence: int = 0
    stopped: bool = False
    #: Set when ``loop`` is attached; ``send_message`` waits for it.
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "running"  # running | idle | interrupted | closed
    last_status: str = "completed"  # completed | error | timeout | interrupted
    mailbox: deque[str] = field(default_factory=deque)
    turn_task: asyncio.Task[Any] | None = None
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    announced: bool = False
    #: A synchronous ``run()`` child: no session file, no sidecar, no registry
    #: entry.  The flag is the routing switch ``_run_turn`` needs (5.3).
    ephemeral: bool = False


class SubAgentRunner:
    """Run one sub-agent task, optionally inside a git worktree sandbox."""

    def __init__(
        self,
        app_config: Any,
        engine_factory: EngineFactory,
        build_tools: BuildTools,
        *,
        ledger_root: str | Path | None = None,
        max_concurrent: int = 4,
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
        #: C-5b-1: where addressable children persist.  ``None`` derives the
        #: isolated ``<agents base>/sessions`` root below, so the main
        #: ``/resume`` list can never pick a child session up.
        session_store: SessionService | None = None,
        #: P2-B2: the process-wide write table the dispatcher also claims
        #: against.  ``None`` keeps the pre-B2 behaviour: no overlap detection.
        write_coordinator: Any | None = None,
        #: P2-B3: the durable settlement lane.  ``None`` derives one under the
        #: workspace (``.ai-coding/notices``); a caller may pass a store rooted
        #: elsewhere (tests), never disable the lane.
        notice_store: NoticeStore | None = None,
        #: P2-C1: the one execution pool shared by tool groups, sync runs,
        #: background runs and follow-up turns.  ``None`` builds one sized by
        #: ``max_concurrent`` so a bare/test runner keeps its old bound.
        slots: ExecutionSlotPool | None = None,
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
        self._write_coordinator = write_coordinator
        self._notice_store = (
            notice_store
            if notice_store is not None
            else NoticeStore.for_workspace(workspace)
        )
        self._slots = (
            slots
            if slots is not None
            else ExecutionSlotPool(max(1, int(max_concurrent)))
        )
        self._slot_by_key: dict[str, Slot] = {}
        #: The scheduler this runner belongs to (set by the builder).  Child
        #: stacks attach it so their loops expose the same facade.
        self.scheduler: Any = None
        # R1-2: background runs tracked across turns: run id -> row, plus the
        # live asyncio tasks (strong refs so they survive the spawning turn),
        # plus the announce context (emit/origin/name) keyed the same way.
        self._background: dict[str, BackgroundTask] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._announce: dict[str, tuple[Any, str, str]] = {}
        # C-5b-1: addressable children (the source of truth) and their on-disk
        # state.  Children live under ``<agents base>/.ai-coding/agents/`` --
        # never at ``ledger_root`` itself (that is the repository root).
        agents_base = (
            Path(session_store.root).parent
            if session_store is not None
            else (
                Path(ledger_root) if ledger_root is not None else Path(workspace)
            )
            / ".ai-coding"
            / "agents"
        )
        self._session_store = (
            session_store
            if session_store is not None
            else SessionService(agents_base / "sessions")
        )
        self._meta_root = agents_base / "meta"
        self._handles: dict[str, AgentHandle] = {}

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
        return self._slots.max_slots

    @property
    def workspace(self) -> str:
        """The main workspace this runner was built for (journal default)."""
        return self._workspace

    @property
    def slots(self) -> ExecutionSlotPool:
        """The one pool this runner shares with tool groups and the scheduler."""
        return self._slots

    @property
    def _active(self) -> set[str]:
        """A read-only view of the keyed slots (kept for old diagnostics)."""
        return self._slots.active_keys

    async def run(
        self,
        task: str | SubagentTask,
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
        origin_session: str = "",
        task_id: str = "",
        fence: int = 0,
    ) -> SubagentResult:
        """Run ``task`` and return its packet.  Never raises for a sub-agent failure.

        ``task`` is either a free-form string (the model-facing tool path and
        old callers) or a :class:`SubagentTask` (the one internal input shape).
        A packet is authoritative for every dispatch field; a string uses the
        keyword arguments, so the pre-P2-A call shape is unchanged.

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
        (
            text,
            request,
            isolate,
            readonly,
            agent,
            requested_mode,
            output_schema,
        ) = self._split_task(
            task,
            isolate=isolate,
            readonly=readonly,
            agent=agent,
            context_mode=context_mode,
            output_schema=output_schema,
        )
        mode = self._resolve_context_mode(self.context_mode, requested_mode)
        # P2-C1: a top-level sync run queues for one pool slot.  A scheduler
        # caller already owns a slot and sets ``slot_held``; asking again would
        # deadlock at ``slots=1``, so the nested path only marks the child.
        key = f"sync:{uuid.uuid4().hex}"
        if not holding_slot():
            await self._admit(key)
        try:
            with enter_child():
                return await self._run_isolated(
                    text, name, parent, parent_span, active,
                    worktree_manager, isolate, output_schema, agent, mode, readonly,
                    request=request, origin_session=origin_session,
                    task_id=task_id, fence=fence,
                )
        finally:
            self._release_slot(key)

    @staticmethod
    def _split_task(
        task: str | SubagentTask,
        *,
        isolate: bool,
        readonly: bool,
        agent: str | None,
        context_mode: str | None,
        output_schema: dict[str, Any] | None,
    ) -> tuple[str, SubagentTask | None, bool, bool, str | None, str | None,
               dict[str, Any] | None]:
        """``(text, request, isolate, readonly, agent, context_mode, schema)``.

        A packet is the single authority for its dispatch fields; a string
        keeps the keyword arguments (tests and pre-P2-A callers).
        """
        if isinstance(task, SubagentTask):
            return (
                task.render(),
                task,
                task.isolate,
                task.readonly,
                task.agent,
                task.context_mode,
                task.output_schema,
            )
        return (task, None, isolate, readonly, agent, context_mode, output_schema)

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

    @staticmethod
    def _should_isolate(isolate: bool, readonly: bool) -> bool:
        """Whether this child gets a worktree.

        A read-only child reads the live working tree and never gets a worktree:
        a worktree is a HEAD snapshot, and isolation buys nothing for a child
        that cannot write.  This is the only place the rule is decided — the
        tools pass ``isolate`` and ``readonly`` through untouched.
        """
        return isolate and not readonly

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
        *,
        request: SubagentTask | None = None,
        origin_session: str = "",
        task_id: str = "",
        fence: int = 0,
    ) -> SubagentResult:
        """Worktree branch shared by the synchronous and background paths.

        Three degradation states, deliberately distinct (P2-A): ``not isolate``
        (which includes every read-only child) and a ``None`` manager mean "no
        worktree was asked for" → ``None``; a configured manager whose root is
        not a git repo → ``worktree_unavailable``; a failed ``create()`` →
        ``worktree_create_failed``.  The old code silently ran both fallbacks in
        the main workspace, so a write delegation could believe it was isolated.
        """
        isolate = self._should_isolate(isolate, readonly)
        degraded: str | None = None
        if isolate and worktree_manager is not None:
            if not await worktree_manager.can_isolate():
                degraded = "worktree_unavailable"
                report_degraded(
                    self._scoped_observer(name, parent_span, observer),
                    "worktree_isolate",
                    "the workspace cannot be isolated (not a git repository)",
                    "ran in the main workspace instead",
                )
            else:
                try:
                    async with worktree_manager.temporary(name) as wt:
                        result = await self._run_in(
                            task, name, str(wt.path), parent,
                            parent_span=parent_span, observer=observer,
                            output_schema=output_schema, agent=agent,
                            context_mode=context_mode, readonly=readonly,
                            request=request, origin_session=origin_session,
                            task_id=task_id, fence=fence,
                        )
                        # r25/v2 + P2-A: the worktree is force-removed on exit,
                        # so a write that is not committed dies with it.  Commit
                        # and carry the branch/commit (or the failure reason)
                        # back to the parent; the directory goes, the branch
                        # stays.
                        result = await self._commit_turn(
                            result, worktree_manager, str(wt.path), name
                        )
                        result = replace(
                            result,
                            base_commit=getattr(wt, "base_commit", None),
                        )
                        if result.commit or result.commit_error:
                            result = replace(result, worktree_branch=wt.branch)
                        _refresh_ledger(result)
                        return result
                except WorktreeError:
                    # Scoped like the child's own events: this record says the
                    # child ran in the *main* workspace, and `report_degraded`
                    # carries no name, so without the wrapper nothing ties it to
                    # a child — and up to three run at once.
                    degraded = "worktree_create_failed"
                    report_degraded(
                        self._scoped_observer(name, parent_span, observer),
                        "worktree_isolate",
                        "the worktree could not be created",
                        "ran in the main workspace instead",
                    )
        result = await self._run_in(
            task, name, None, parent, parent_span=parent_span, observer=observer,
            output_schema=output_schema, agent=agent,
            context_mode=context_mode, readonly=readonly,
            request=request, origin_session=origin_session,
            task_id=task_id, fence=fence,
        )
        if degraded:
            result = replace(result, degraded=degraded)
            # P2-A: ``_run_turn`` wrote the ledger before this degradation was
            # known, and the non-git path has no branch to trigger the usual
            # refresh -- merge it here or the file keeps ``degraded: null``.
            _refresh_ledger(result)
        return result

    # ---- background runs (R1-2) -------------------------------------------

    def spawn_background(
        self,
        task: str | SubagentTask,
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
        context_mode: str | None = None,
        readonly: bool = False,
        slot: Slot | None = None,
        task_id: str = "",
        fence: int = 0,
    ) -> str:
        """Start a child without waiting; return its run id at once.

        ``task`` accepts the same ``str | SubagentTask`` union as :meth:`run`;
        a packet is authoritative for its dispatch fields.

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
        # P2-B2: admission is one set, not a count of rows -- a row may outlive
        # its slot, and a continuation turn occupies a slot without a new row.
        # The semaphore stays the hard execution bound.
        (
            text,
            request,
            isolate,
            readonly,
            agent,
            requested_mode,
            output_schema,
        ) = self._split_task(
            task,
            isolate=isolate,
            readonly=readonly,
            agent=agent,
            context_mode=context_mode,
            output_schema=output_schema,
        )
        run_id = uuid.uuid4().hex[:12]
        if slot is not None:
            # The scheduler already reserved the pool entry; this run owns it
            # now, so every terminal path releases the same lease.
            slot.bind(run_id)
            self._slot_by_key[run_id] = slot
        elif not self.try_admit(run_id):
            raise SubAgentPoolFull(
                "sub-agent concurrency exhausted: "
                f"{self._slots.max_slots} background slots busy; "
                "retry later or raise ai.max_concurrent_subagents"
            )
        active = self._observer if observer is None else observer
        mode = self._resolve_context_mode(self.context_mode, requested_mode)
        self._background[run_id] = BackgroundTask(
            run_id=run_id, name=name, task=text, status="running",
            started=time.monotonic(),
        )
        self._prune_background()
        # C-5b-1: the handle is opened *before* the task starts, so the row is
        # addressable from the moment ``spawn_background`` returns.  A failure
        # here never leaves a ``running`` row behind.
        try:
            handle = self._open_handle(
                text, name, None, parent,
                parent_span=parent_span, observer=active,
                output_schema=output_schema, agent=agent,
                context_mode=mode, readonly=readonly,
                run_id=run_id, ephemeral=False, defer_loop=True,
                request=request, origin_session=origin_session,
                task_id=task_id, fence=fence,
            )
        except Exception as exc:  # noqa: BLE001 — a row must not stay "running"
            # P2-B2: a start that never produced a run must give the slot back.
            self._release_slot(run_id)
            self._background[run_id] = replace(
                self._background[run_id],
                status="error",
                ended=time.monotonic(),
                error=f"failed to start: {exc}",
            )
            raise RuntimeError(f"sub-agent failed to start: {exc}") from exc
        handle.emit = emit
        self._background[run_id] = replace(
            self._background[run_id], session_id=handle.session.session_id
        )
        self._write_meta(handle)
        supervised = self._supervised(
            handle, text,
            worktree_manager=worktree_manager,
            isolate=isolate,
        )
        try:
            background_task = asyncio.get_running_loop().create_task(
                supervised, name=f"subagent:{run_id}"
            )
        except Exception as exc:  # noqa: BLE001 — a row must not stay "running"
            # C-5a review: the row used to be written before the task existed.
            # If the task could not be created, the row stayed ``running``
            # forever -- un-cancellable, and ``wait`` never returned.  The
            # coroutine must be closed explicitly or Python reports it as never
            # awaited.
            supervised.close()
            # P2-B2: same reason as the handle failure above.
            self._release_slot(run_id)
            handle.status = "closed"
            self._handles.pop(run_id, None)
            self._background[run_id] = replace(
                self._background[run_id],
                status="error",
                ended=time.monotonic(),
                error=f"failed to start: {exc}",
            )
            raise RuntimeError(f"sub-agent failed to start: {exc}") from exc
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
        # C-5b-1 handles-first: an id that still has a handle is addressable
        # even when its projection row was pruned.
        resolved = self.resolve_run_id(run_id) or run_id
        row = self._background.get(resolved)
        background_task = self._tasks.get(resolved)
        if row is None or background_task is None or row.status != "running":
            return False
        background_task.cancel()
        return True

    async def stop_run(self, run_id: str) -> bool:
        """Kill a run for good: cancel, clear mailbox, latch ``stopped``.

        Unlike :meth:`cancel`/``interrupt``, a stopped child never starts
        another turn: queued messages are dropped, ``send_message`` answers
        ``stopped`` and ``resume`` refuses until the operator reopens it
        explicitly (C2).
        """
        resolved = self.resolve_run_id(run_id) or run_id
        handle = self._handles.get(resolved)
        if handle is None:
            return self.cancel(resolved)
        handle.stopped = True
        handle.mailbox.clear()
        task = handle.turn_task or self._tasks.get(resolved)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        loop = handle.loop
        handle.loop = None
        if loop is not None:
            try:
                await loop.aclose()
            except Exception:  # noqa: BLE001 — the terminal state is what matters
                _log.debug("child loop close after stop failed", exc_info=True)
        handle.status = "stopped"
        row = self._background.get(resolved)
        if row is not None:
            self._background[resolved] = replace(
                row, status="cancelled", ended=time.monotonic()
            )
        self._release_slot(resolved)
        self._tasks.pop(resolved, None)
        self._announce.pop(resolved, None)
        if not handle.ephemeral:
            self._session_store.save(handle.session)
            self._write_meta(handle)
        return True

    # ---- C-5b: send / interrupt / resume / close ---------------------------

    def resolve_target(self, target: str) -> tuple[str, str, list[str]]:
        """Classify an address: ``(kind, run_id, candidates)``.

        ``kind`` is ``ok`` (one live handle), ``unknown`` (nothing matches or it
        is closed) or ``ambiguous`` (a bare name matches several live children).
        The tools need the third case: silently answering "unknown" for a name
        the operator can see in ``list_agents`` is the least useful reply.
        """
        if not target:
            return "unknown", "", []
        resolved = self.resolve_run_id(target)
        if resolved is not None:
            handle = self._handles.get(resolved)
            if handle is not None and handle.status != "closed":
                return "ok", resolved, []
            return "unknown", "", []
        matches = [
            handle
            for handle in self._handles.values()
            if handle.name == target and handle.status != "closed"
        ]
        if len(matches) == 1:
            return "ok", matches[0].run_id, []
        if len(matches) > 1:
            return "ambiguous", "", [handle.run_id for handle in matches]
        return "unknown", "", []

    def _handle_for(self, target: str) -> AgentHandle | None:
        """The live handle a target names -- id prefix first, then a unique name.

        Addressing is handles-first (4.1): the projection row may be pruned and
        a closed handle is not addressable at all.  An ambiguous name resolves
        to ``None`` rather than guessing which child the operator meant.
        """
        kind, run_id, _candidates = self.resolve_target(target)
        return self._handles.get(run_id) if kind == "ok" else None

    async def send_message(self, target: str, text: str) -> tuple[str, str]:
        """Deliver ``text`` to a child; return ``(status, run_id)``.

        A running turn takes the message into its mailbox and reads it at the
        next iteration boundary; an idle/interrupted handle gets a new turn
        instead (our one wake-up path, design §9).  Unknown twice over: no
        handle, and never a run id to name it by.
        """
        handle = self._handle_for(target)
        if handle is None:
            return "unknown", ""
        if handle.stopped:
            return "stopped", handle.run_id
        task = handle.turn_task
        if (
            (task is not None and not task.done())
            or handle.status in {"running", "interrupting"}
        ):
            handle.mailbox.append(text)
            return "queued", handle.run_id
        self._schedule_turn(handle, text)
        return "running", handle.run_id

    def _schedule_turn(self, handle: AgentHandle, text: str) -> None:
        """Start one turn on an idle handle, without an await before the slot.

        The check "is a turn already assigned?" and the assignment are in the
        same synchronous block on purpose (DF-2): a race here would overwrite
        ``turn_task`` and run two turns against one session.
        """
        if handle.stopped:
            raise SubAgentPoolFull("child was stopped")
        if not self.try_admit(handle.run_id):
            raise SubAgentPoolFull(
                "sub-agent concurrency exhausted: "
                f"{self._slots.max_slots} background slots busy; "
                "retry later or raise ai.max_concurrent_subagents"
            )
        handle.status = "running"
        handle.announced = False
        row = self._background.get(handle.run_id)
        if row is not None:
            self._background[handle.run_id] = replace(
                row, status="running", ended=None, error=None, usage=None,
                ledger_path=None,
            )
        followup = self._run_followup(handle, text)
        try:
            task = asyncio.get_running_loop().create_task(
                followup, name=f"subagent:{handle.run_id}"
            )
        except Exception as exc:
            # P2-B2: a turn that never became a task must not hold its slot, and
            # its row must not stay ``running`` -- nothing would ever settle it.
            followup.close()
            self._release_slot(handle.run_id)
            handle.status = "idle"
            handle.turn_task = None
            row = self._background.get(handle.run_id)
            if row is not None:
                self._background[handle.run_id] = replace(
                    row,
                    status="error",
                    ended=time.monotonic(),
                    error=f"failed to start: {exc}",
                )
            raise
        handle.turn_task = task
        self._tasks[handle.run_id] = task
        task.add_done_callback(functools.partial(self._reap, handle.run_id))

    async def _run_followup(self, handle: AgentHandle, text: str) -> None:
        """One continuation turn: run it, settle it, then drain the mailbox.

        Cancellation is a **status**, not an exit (DF-3): the inner turn is
        awaited shut by ``_run_turn``, the session is saved, the handle stays
        (so ``resume_agent`` can reopen it), and no completion notice is sent.
        """
        interrupted = False
        try:
            # P2-C1: ``_schedule_turn`` reserved this turn's slot.
            with enter_child():
                if handle.loop is None:
                    await self._attach_loop(
                        handle, handle.worktree_path or handle.workdir
                    )
                result = await self._run_turn(handle, text)
        except asyncio.CancelledError:
            interrupted = True
            # P2-B2: the interrupted turn had a slot from ``_schedule_turn``.
            self._release_slot(handle.run_id)
            handle.status = "interrupted"
            handle.last_status = "interrupted"
            row = self._background.get(handle.run_id)
            if row is not None:
                self._background[handle.run_id] = replace(
                    row, status="interrupted", ended=time.monotonic()
                )
            self._tasks.pop(handle.run_id, None)
            self._announce.pop(handle.run_id, None)
            loop = handle.loop
            handle.loop = None
            if not handle.ephemeral:
                self._session_store.save(handle.session)
                self._write_meta(handle)
            if loop is not None:
                # The cancelled turn is closed; the engine behind it is not
                # reusable after a cut, so release it here and rebuild later.
                try:
                    await loop.aclose()
                except Exception:  # noqa: BLE001 — release must not mask the cut
                    _log.debug("child loop close after interrupt failed", exc_info=True)
        else:
            handle.status = "idle"
            result = await self._commit_turn(
                result,
                handle.worktree_manager,
                handle.worktree_path,
                handle.name,
            )
            result = self._with_handle_branch(handle, result)
            # P2-A: refresh unconditionally -- a non-git child has no branch,
            # but its degraded/commit/changed_paths must still reach the ledger.
            _refresh_ledger(result)
            self._record_handle_facts(handle, result)
            self._write_meta(handle)
            self._finish_background(
                handle.run_id, result, handle.emit, handle.origin_session
            )
        finally:
            current = asyncio.current_task()
            if handle.turn_task is current:
                handle.turn_task = None
        if interrupted or handle.stopped or handle.status == "closed":
            return
        # A message may land after the last iteration boundary; run it as one
        # merged follow-up turn instead of leaving it parked in the mailbox.
        remaining = list(handle.mailbox)
        if remaining:
            try:
                self._schedule_turn(handle, "\n".join(remaining))
            except SubAgentPoolFull:
                # Keep them queued: a saturated pool is a retry, not a drop.
                _log.debug(
                    "follow-up turn for %s rejected: pool full", handle.run_id,
                    exc_info=True,
                )
            else:
                _drain_mailbox(handle.mailbox)

    async def interrupt(self, target: str) -> tuple[str, str]:
        """Cancel a child's current turn and keep everything else.

        A handle with no live turn is a no-op that reports its status; an
        unknown or closed one is ``not_found`` (the tool still succeeds -- Codex
        treats "already dead" as a successful interrupt).  The returned
        ``previous`` status is what the caller prints before the arrow.
        """
        handle = self._handle_for(target)
        if handle is None:
            return "not_found", target
        # P2-B2: an interrupt ends the turn's admission; idempotent with the
        # follow-up's own release and the reaper's callback.
        self._release_slot(handle.run_id)
        task = handle.turn_task
        if task is None or task.done():
            # C-5b review: the first turn lives in ``_tasks`` until the
            # supervised body returns; without this fallback a just-spawned
            # agent could not be interrupted at all.
            task = self._tasks.get(handle.run_id)
        if task is None or task.done():
            return handle.status, handle.status
        previous = handle.status
        handle.status = "interrupting"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        handle.status = "interrupted"
        handle.last_status = "interrupted"
        loop = handle.loop
        handle.loop = None
        if loop is not None:
            try:
                await loop.aclose()
            except Exception:  # noqa: BLE001 — release must not mask the cut
                _log.debug("child loop close after interrupt failed", exc_info=True)
        if not handle.ephemeral:
            self._session_store.save(handle.session)
            self._write_meta(handle)
        row = self._background.get(handle.run_id)
        if row is not None:
            self._background[handle.run_id] = replace(
                row, status="interrupted", ended=time.monotonic()
            )
        return previous, "interrupted"

    async def resume(self, target: str) -> tuple[str, str]:
        """Make a child usable again; never starts a turn (Codex semantics).

        Two layers: an in-memory handle only needs its loop back, while a
        closed one is reopened from its sidecar + session file.  A running
        handle is a no-op.  Resumed state deliberately drops every runtime
        marker the session file carried (stale approvals, write-ahead records,
        an open turn, queued reminders), so nothing from before the cut leaks
        into the new turn.
        """
        handle = self._handle_for(target)
        if handle is not None:
            if handle.stopped:
                return "stopped", handle.run_id
            task = handle.turn_task
            if handle.status == "running" or (task is not None and not task.done()):
                return "running", handle.run_id
            if handle.loop is None:
                await self._attach_loop(
                    handle, handle.worktree_path or handle.workdir
                )
            self._clear_runtime_markers(handle.session)
            handle.status = "idle"
            if not handle.ephemeral:
                self._session_store.save(handle.session)
                self._write_meta(handle)
            self._restore_row(handle)
            return "idle", handle.run_id
        # A cross-process target is a full id this process never saw, so the
        # literal id is the fallback -- the sidecar file is the address then.
        run_id = self.resolve_run_id(target) or target
        meta = self._load_meta(run_id)
        if meta is None:
            return "not_found", target
        session = self._session_store.load(str(meta.get("sessionId") or ""))
        if session is None:
            return "not_found", target
        self._clear_runtime_markers(session)
        rules, _errors = rules_from_dicts(list(meta.get("permissions") or []))
        workdir = meta.get("workdir") or meta.get("workspace")
        if workdir and not Path(str(workdir)).exists():
            # A closed child's temporary/persistent worktree may have been
            # removed; resuming in a vanished directory would fail every tool.
            workdir = None
        raw_task = meta.get("taskPacket")
        request = (
            SubagentTask.from_dict(raw_task) if isinstance(raw_task, dict) else None
        )
        raw_schema = meta.get("outputSchema")
        output_schema = dict(raw_schema) if isinstance(raw_schema, dict) else None
        handle = AgentHandle(
            run_id=run_id,
            name=str(meta.get("name") or "sub-agent"),
            task=str(meta.get("task") or ""),
            session=session,
            session_store=self._session_store,
            loop=None,
            workdir=workdir,
            readonly=bool(meta.get("readonly")),
            agent=meta.get("agent"),
            context_mode=str(meta.get("contextMode") or "full"),
            parent_permissions=tuple(rules),
            parent_span="",
            emit=None,
            origin_session=str(meta.get("parentSessionId") or ""),
            output_schema=output_schema,
            request=request,
            worktree_branch=(
                str(meta["worktreeBranch"])
                if meta.get("worktreeBranch") is not None
                else None
            ),
            commit=str(meta["commit"]) if meta.get("commit") is not None else None,
            base_commit=(
                str(meta["baseCommit"])
                if meta.get("baseCommit") is not None
                else None
            ),
            commit_error=(
                str(meta["commitError"])
                if meta.get("commitError") is not None
                else None
            ),
            degraded=(
                str(meta["degraded"]) if meta.get("degraded") is not None else None
            ),
            status="idle",
            last_status=str(meta.get("lastStatus") or "completed"),
        )
        await self._attach_loop(handle, handle.worktree_path or handle.workdir)
        self._handles[run_id] = handle
        self._restore_row(handle)
        self._write_meta(handle)
        return "idle", run_id

    async def close(self, target: str) -> bool:
        """Close a child for good: cancel its turn, close its loop, drop it.

        The disk state survives (session + sidecar), which is what makes
        ``resume_agent`` able to reopen it.  A closed child never announces:
        closing is the operator's own act, not a completion.
        """
        handle = self._handle_for(target)
        if handle is None:
            return False
        # P2-B2: closing cancels the turn, so its admission ends here too.
        self._release_slot(handle.run_id)
        task = handle.turn_task
        if task is None or task.done():
            # The spawning turn lives in ``_tasks`` (its slot is ``turn_task``
            # only for continuation turns); closing must stop it all the same.
            task = self._tasks.get(handle.run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A turn that queued a follow-up while the first one was being cut.
        followup = handle.turn_task
        if followup is not None and not followup.done():
            followup.cancel()
            await asyncio.gather(followup, return_exceptions=True)
        handle.status = "closed"
        loop = handle.loop
        handle.loop = None
        if loop is not None:
            try:
                await loop.aclose()
            except Exception:  # noqa: BLE001 — closing must not fail on a dead loop
                _log.debug("child loop close failed", exc_info=True)
        # P2-A: the last commit is the operator's safety net, and its sha must
        # reach the sidecar *before* the worktree is deleted.  A failure is
        # recorded, never swallowed into "no commit".
        worktree_path = handle.worktree_path
        if handle.worktree_manager is not None and worktree_path:
            try:
                sha, error = await handle.worktree_manager.commit_if_dirty(
                    worktree_path, f"subagent: {handle.name}"
                )
                if sha:
                    handle.commit = sha
                    handle.commit_error = None
                elif error:
                    handle.commit_error = error
            except Exception:  # noqa: BLE001 — the disk state is what matters
                _log.debug("child worktree commit failed", exc_info=True)
        if not handle.ephemeral:
            self._session_store.save(handle.session)
            self._write_meta(handle)
        if handle.worktree_manager is not None and worktree_path:
            try:
                await handle.worktree_manager.remove(worktree_path)
            except Exception:  # noqa: BLE001 — the disk state is what matters
                _log.debug("child worktree cleanup failed", exc_info=True)
        self._handles.pop(handle.run_id, None)
        self._tasks.pop(handle.run_id, None)
        self._announce.pop(handle.run_id, None)
        row = self._background.get(handle.run_id)
        if row is not None:
            self._background[handle.run_id] = replace(
                row, status="cancelled", ended=time.monotonic()
            )
        return True

    def close_info(self, run_id: str) -> dict[str, Any]:
        """The commit facts a closed child left in its sidecar (tools echo them).

        Read from disk on purpose: ``close`` removes the worktree, and the
        handle is gone from ``_handles``; the meta file is the durable record.
        """
        meta = self._load_meta(run_id)
        if meta is None:
            return {}
        return {
            "worktreeBranch": meta.get("worktreeBranch"),
            "commit": meta.get("commit"),
            "commitError": meta.get("commitError"),
            "baseCommit": meta.get("baseCommit"),
        }

    def _restore_row(self, handle: AgentHandle) -> None:
        """Keep ``/tasks`` in step with a resumed handle (row = projection)."""
        row = self._background.get(handle.run_id)
        if row is None:
            self._background[handle.run_id] = BackgroundTask(
                run_id=handle.run_id,
                name=handle.name,
                task=handle.task,
                status=handle.status,
                started=time.monotonic(),
                session_id=handle.session.session_id,
                worktree_branch=handle.worktree_branch,
                commit=handle.commit,
                base_commit=handle.base_commit,
                commit_error=handle.commit_error,
                degraded=handle.degraded,
            )
            self._prune_background()
            return
        self._background[handle.run_id] = replace(
            row,
            status=handle.status,
            ended=None,
            error=None,
            worktree_branch=row.worktree_branch or handle.worktree_branch,
            commit=row.commit or handle.commit,
            base_commit=row.base_commit or handle.base_commit,
            commit_error=handle.commit_error or row.commit_error,
            degraded=handle.degraded or row.degraded,
        )

    async def wait(self, run_id: str, timeout_ms: int = 30_000) -> tuple[str, int]:
        """Wait for one background run; never cancels it. Returns (status, elapsed_ms).

        ``status`` is the table row's status (``running`` when the wait timed
        out, ``unknown`` when the id is not in the table).  The child keeps
        running either way: the wait is an observation, not a kill switch —
        cancelling it is ``close_agent``'s job.
        """
        row = self._background.get(run_id)
        if row is None:
            return "unknown", 0
        started = row.started
        background_task = self._tasks.get(run_id)
        if background_task is not None and not background_task.done():
            # C-5a review: ``wait_for(shield(task))`` still re-raises the child's
            # CancelledError in the waiter, which aborts the *parent* turn.
            # ``asyncio.wait`` observes completion without forwarding the child's
            # exception, and a timeout is only an observation -- the child keeps
            # running either way.
            timeout = max(0.0, min(_MAX_WAIT_SECONDS, int(timeout_ms) / 1000))
            try:
                await asyncio.wait({background_task}, timeout=timeout)
            except asyncio.CancelledError:
                raise
        current = self._background.get(run_id) or row
        if current.status == "running" and background_task is not None:
            if background_task.done():
                # The settlement callback may not have run yet; do not report a
                # finished task as still running.
                if background_task.cancelled():
                    current = replace(current, status="cancelled")
                elif background_task.exception() is not None:
                    current = replace(current, status="error")
        elapsed_ms = int(((current.ended or time.monotonic()) - started) * 1000)
        return current.status, max(0, elapsed_ms)

    def resolve_run_id(self, prefix: str) -> str | None:
        """The full run id a printed prefix stands for, or None.

        ``/tasks`` shows ``run_id[:12]`` and ``cancel`` needs the whole thing;
        without this bridge the operator can only copy something that does not
        work.  Ambiguous prefixes resolve to None (the caller lists candidates)
        — the same rule ``/resume`` follows via ``SessionService.resolve``.
        """
        if not prefix:
            return None
        # C-5b-1 handles-first: the handle is the source of truth, so an id
        # whose projection row was pruned still resolves.  An ambiguous
        # handle prefix is an ambiguity, not a reason to fall through.
        if prefix in self._handles:
            return prefix
        handle_matches = [key for key in self._handles if key.startswith(prefix)]
        if len(handle_matches) > 1:
            return None
        if len(handle_matches) == 1:
            return handle_matches[0]
        exact = self._background.get(prefix)
        if exact is not None:
            return prefix
        matches = [key for key in self._background if key.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def task_list(self) -> list[BackgroundTask]:
        """Background rows, oldest first (running and settled alike)."""
        return sorted(self._background.values(), key=lambda row: row.started)

    async def _admit(self, key: str) -> None:
        """Wait for one slot, then take ``key`` (sync runs queue, never refuse)."""
        slot = await self._slots.acquire(key, key=key)
        self._slot_by_key[key] = slot

    def try_admit(self, key: str) -> bool:
        """Take a slot without waiting; ``False`` means the pool is full."""
        slot = self._slots.try_reserve(key, key=key)
        if slot is None:
            return False
        self._slot_by_key[key] = slot
        return True

    def _release_slot(self, key: str) -> None:
        """Surrender ``key``'s slot; idempotent across every terminal path."""
        slot = self._slot_by_key.pop(key, None)
        if slot is not None:
            slot.release()

    def _prune_background(self) -> None:
        """Bound the table: oldest terminal rows go first, running never.

        C-5b-1: a row whose run id still has a handle is never pruned — the
        handle stays addressable after it settles, and dropping its row would
        make ``/tasks``/``list_agents`` forget a child that is still usable.
        """
        terminal = sorted(
            (
                row
                for row in self._background.values()
                if row.status != "running" and row.run_id not in self._handles
            ),
            key=lambda row: row.started,
        )
        while len(self._background) > _MAX_BACKGROUND_ROWS and terminal:
            self._background.pop(terminal.pop(0).run_id, None)

    async def _supervised(
        self,
        handle: AgentHandle,
        text: str,
        *,
        worktree_manager: Any | None = None,
        isolate: bool = True,
    ) -> None:
        """Run one background child's first turn, then announce it.

        Never raises: an unretrieved task exception is noise at shutdown, so
        every failure mode below ends in a table row plus (best-effort) event.
        The handle is kept (``status=idle``) and its loop is deliberately not
        closed: C-5b-2+ continues the same conversation.
        """
        isolate = self._should_isolate(isolate, handle.readonly)
        degraded: str | None = None
        try:
            # P2-C1: the spawn reserved this run's pool slot; the child's own
            # tool groups inherit ``slot_held`` and never ask for another.
            with enter_child():
                degraded = await self._attach_for_run(handle, worktree_manager, isolate)
                result = await self._run_turn(handle, text)
        except asyncio.CancelledError:
            # C-5b: a cut may be the operator's `interrupt_agent` (the handle
            # is already "interrupting") or a plain close; the two report
            # differently and only the latter may announce.
            interrupted = handle.status in ("interrupting", "interrupted")
            result = SubagentResult(
                status="interrupted" if interrupted else "cancelled",
                error="interrupted by user" if interrupted else "cancelled by user",
                truncated=True, exit_reason="interrupted" if interrupted else "cancelled",
            )
            loop = handle.loop
            handle.loop = None
            if loop is not None:
                try:
                    await loop.aclose()
                except Exception:  # noqa: BLE001 — release must not mask the cut
                    _log.debug("child loop close after interrupt failed", exc_info=True)
        except Exception as exc:  # noqa: BLE001 — _run_in promises never-raise; belt.
            result = SubagentResult(
                status="error", error=f"supervisor: {exc}",
                truncated=True, exit_reason="supervisor_error",
            )
        if degraded:
            result = replace(result, degraded=degraded)
        # P2-A: commit at the end of every background turn, not only at close.
        # A settlement that precedes the commit told the parent "done" while the
        # work still lived only in a directory ``close_agent`` would delete.
        result = await self._commit_turn(
            result, handle.worktree_manager, handle.worktree_path, handle.name
        )
        result = self._with_handle_branch(handle, result)
        # P2-A: refresh unconditionally -- see ``_run_followup``; a degraded
        # non-git turn has no branch, but its facts still belong in the file.
        _refresh_ledger(result)
        self._record_handle_facts(handle, result)
        if handle.status == "interrupting":
            handle.status = "interrupted"
        elif result.status != "interrupted":
            handle.status = "idle"
        self._write_meta(handle)
        self._finish_background(
            handle.run_id, result, handle.emit, handle.origin_session
        )
        if handle.stopped:
            return  # a stopped child never restarts from a queued mailbox
        # A message queued after the last iteration boundary must not sit in the
        # mailbox forever -- run it as one merged follow-up turn (DF-2).
        remaining = list(handle.mailbox)
        if remaining:
            try:
                self._schedule_turn(handle, "\n".join(remaining))
            except SubAgentPoolFull:
                _log.debug(
                    "follow-up turn for %s rejected: pool full", handle.run_id,
                    exc_info=True,
                )
            else:
                _drain_mailbox(handle.mailbox)

    async def _attach_for_run(
        self, handle: AgentHandle, worktree_manager: Any | None, isolate: bool
    ) -> str | None:
        """Attach the loop to a persistent worktree (or the main workspace).

        Returns the degradation reason, or ``None``.  The three states match
        the sync path's (P2-A): no isolation asked for / no manager configured
        → ``None``; a manager that cannot isolate → ``worktree_unavailable``;
        a failed ``create()`` → ``worktree_create_failed``.

        C-5b-1 opened the handle with a deferred loop so the worktree can be
        chosen *before* the engine/tools are built.  A temporary worktree would
        be removed before the next turn, so addressable children use a
        persistent one; ``close_agent`` commits and removes it.
        """
        if worktree_manager is None or not isolate:
            await self._attach_loop(handle, None)
            return None
        if not await worktree_manager.can_isolate():
            report_degraded(
                self._scoped_observer(
                    handle.name, handle.parent_span, handle.observer
                ),
                "worktree_isolate",
                "the workspace cannot be isolated (not a git repository)",
                "ran in the main workspace instead",
            )
            await self._attach_loop(handle, None)
            return "worktree_unavailable"
        try:
            wt = await worktree_manager.create(handle.name)
        except WorktreeError:
            report_degraded(
                self._scoped_observer(
                    handle.name, handle.parent_span, handle.observer
                ),
                "worktree_isolate",
                "the worktree could not be created",
                "ran in the main workspace instead",
            )
            await self._attach_loop(handle, None)
            return "worktree_create_failed"
        handle.worktree_path = str(wt.path)
        handle.worktree_branch = wt.branch
        handle.base_commit = getattr(wt, "base_commit", None)
        handle.worktree_manager = worktree_manager
        await self._attach_loop(handle, str(wt.path))
        return None

    def _finish_background(
        self,
        run_id: str,
        result: SubagentResult,
        emit: Any,
        origin_session: str,
        notify: bool = True,
    ) -> None:
        """Settle the row, drop the task ref, announce best-effort.

        ``notify`` is False for endings that must stay silent (DF-6): an
        interrupt or a close is the operator's own act, not a completion, and
        a notice for it would read as "the child finished".

        P2-A: row, notice and event are one projection of ``result``; the
        event's ``details`` is that projection, never the full conclusion.
        """
        # P2-B2: release at the very top, before any row-state check -- a
        # settlement that arrives twice (finish + reap) must free exactly one
        # slot, and a row already settled elsewhere must not leak one.
        self._release_slot(run_id)
        row = self._background.get(run_id)
        self._tasks.pop(run_id, None)
        self._announce.pop(run_id, None)
        handle = self._handles.get(run_id)
        if handle is not None and not self._scheduler_accepts(handle):
            # A stopped/cancelled task's late result must not rewrite the row
            # or emit a completion notice.
            return
        if row is None or row.status != "running":
            return
        summary = _summary_of(result)
        commit_error = (
            result.commit_error
            if result.commit_error
            else (None if result.commit else row.commit_error)
        )
        self._background[run_id] = replace(
            row, status=result.status, ended=time.monotonic(),
            error=result.error, usage=result.usage,
            # C-3b: the pointer subagent_list hands out once the notice may be
            # gone (the ledger file itself was written by ``_run_turn``).
            ledger_path=result.ledger_path,
            # A later turn that commits nothing must not erase the facts an
            # earlier turn earned: the row is the cross-turn memory here.
            worktree_branch=result.worktree_branch or row.worktree_branch,
            commit=result.commit or row.commit,
            base_commit=result.base_commit or row.base_commit,
            changed_paths=result.changed_paths,
            commit_error=commit_error,
            degraded=result.degraded or row.degraded,
        )
        # DF-6: only a cut/close stays silent.  ``completed``/``error``/
        # ``timeout``/``max_iterations`` are real settlements the parent must
        # hear about (the narrower earlier set silently dropped max_iterations).
        self._scheduler_event(
            handle, result, pending=bool(handle.mailbox) if handle is not None else False
        )
        if not notify or result.status in {"cancelled", "interrupted", "closed"}:
            return
        # C-5a review: the model lane is not an observation.  Announcing it was
        # gated behind ``emit is None``, so a runner without an operator stream
        # (the bare CLI, a test double) silently dropped the settlement the
        # parent turn was waiting for.  Announce first; the event stays optional.
        self._announce_to_model(run_id, row.name, result, origin_session)
        if emit is None:
            return
        try:
            emit(BackgroundTaskDone(
                run_id=run_id, name=row.name, status=result.status,
                summary=summary, truncated=result.truncated,
                origin_session=origin_session,
                # R1-3: the pointer the event docstring always promised — the
                # full packet (facts included) lives in this file.  Without
                # it a background+schema run's facts would be unreachable.
                ledger_path=result.ledger_path,
                # P2-A: a crop of the settlement, never conclusion/task.
                details=_details_projection(result),
            ))
        except Exception:  # noqa: BLE001 — observation must not break settlement
            _log.debug("background announce failed for %s", run_id, exc_info=True)

    def _announce_to_model(
        self,
        run_id: str,
        name: str,
        result: SubagentResult,
        origin_session: str = "",
    ) -> None:
        """Hand the settlement to the parent's loop (P0-2 / P2-B3).

        Separate from the event on purpose: the event is an observation the
        operator may miss, and a failed write or ``put`` must never disturb a
        settlement that has already been recorded on the row.

        The same line goes to disk (keyed by the spawning session) and to the
        in-memory lane.  Disk is the restart-safe half; the queue is the
        running-turn half.  ``wake=True`` lets an idle server session start a
        turn for a settlement; a bare CLI has no ``on_put`` and simply waits
        for the next human input.
        """
        line = clip_notice(_notice_text(run_id, name, result))
        if self._notice_store is not None and origin_session:
            try:
                self._notice_store.append(origin_session, line)
            except Exception:  # noqa: BLE001 — the in-memory lane still speaks
                _log.debug(
                    "background notice persist failed for %s", run_id, exc_info=True
                )
        if self._notices is None:
            return
        try:
            self._notices.put(line, wake=True)
        except Exception:  # noqa: BLE001 — same reason as the event above
            _log.debug("background notice failed for %s", run_id, exc_info=True)

    def _reap(self, run_id: str, _task: asyncio.Task[None] | None = None) -> None:
        """Settle a row whose task died without going through ``_finish``.

        A task cancelled before its first step never runs the supervised body
        at all — without this, ``cancel()`` right after spawn would leave the
        row ``running`` forever.  Runs exactly once per task (done-callback);
        a no-op unless the row is still live.

        C-5b: a continuation turn registers the same callback, and the *old*
        task's callback can fire after the next turn already started.  The
        registered-task identity check is what keeps the stale callback from
        settling a live row.
        """
        current = self._tasks.get(run_id)
        if _task is not None and current is not None and current is not _task:
            return
        # P2-B2: after the stale-task guard and before the row guard -- an old
        # callback must never free the slot a newer follow-up turn holds, and a
        # task cancelled before its first step has no other release path.
        self._release_slot(run_id)
        row = self._background.get(run_id)
        self._tasks.pop(run_id, None)
        announce = self._announce.pop(run_id, None)
        handle = self._handles.get(run_id)
        if handle is not None and not self._scheduler_accepts(handle):
            return
        if row is None or row.status != "running":
            return
        # C-5a review: every non-Exception death used to be reported as
        # "cancelled before start", masking a real crash.  Read the task's own
        # outcome: cancelled is cancelled; a swallowed exception is an error.
        status, error = "cancelled", "cancelled before start"
        if _task is not None and not _task.cancelled():
            failure = _task.exception()
            if failure is not None:
                status, error = "error", f"crashed before settlement: {failure}"
        result = SubagentResult(
            status=status, error=error, truncated=True, exit_reason=status,
        )
        self._background[run_id] = replace(
            row, status=status, ended=time.monotonic(), error=error,
        )
        handle = self._handles.get(run_id)
        if handle is not None:
            if handle.status in ("interrupted", "interrupting", "closed"):
                # C-5b: a cut or a close is not a settlement to report, and
                # its status must not be overwritten by this fallback.
                return
            handle.status = "idle"
            self._write_meta(handle)
        if announce is None:
            # C-5b review: ``_announce`` is popped by the previous turn's
            # ``_finish_background``; a crash in a later turn still has to
            # reach the parent, and the handle carries where to send it.
            if handle is None:
                return
            announce = (handle.emit, handle.origin_session, handle.name)
        emit, origin_session, name = announce
        if handle is not None:
            self._scheduler_event(handle, result)
        self._announce_to_model(run_id, name, result, origin_session)
        if emit is None:
            return
        try:
            emit(BackgroundTaskDone(
                run_id=run_id, name=name, status=status,
                summary=error, truncated=True,
                origin_session=origin_session,
                details={
                    "v": SUBAGENT_PACKET_VERSION,
                    "messageId": None,
                    "status": status,
                    "error": error,
                },
            ))
        except Exception:  # noqa: BLE001 — observation must not break settlement
            _log.debug("background reap announce failed for %s", run_id, exc_info=True)

    # ---- internals --------------------------------------------------------

    def _scheduler_accepts(self, handle: AgentHandle | None) -> bool:
        """Whether a late settlement still owns this task (fence gate)."""
        scheduler = self.scheduler
        if scheduler is None or handle is None or not handle.task_id:
            return True
        accepts = getattr(scheduler, "accepts", None)
        return bool(accepts(handle.task_id, handle.fence)) if callable(accepts) else True

    def _scheduler_event(
        self, handle: AgentHandle | None, result: SubagentResult, *, pending: bool = False
    ) -> None:
        scheduler = self.scheduler
        if scheduler is None or handle is None or not handle.task_id:
            return
        event = getattr(scheduler, "on_runner_event", None)
        if callable(event):
            event(
                handle.task_id,
                handle.run_id,
                result.status,
                handle.fence,
                pending=pending,
                branch=result.worktree_branch or handle.worktree_branch,
                commit=result.commit or handle.commit,
                base_commit=result.base_commit or handle.base_commit,
                commit_error=result.commit_error or handle.commit_error,
            )

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
        request: SubagentTask | None = None,
        origin_session: str = "",
        task_id: str = "",
        fence: int = 0,
    ) -> SubagentResult:
        """The one-shot path for ``run()`` / fan-out (5.3).

        It is an **ephemeral** handle: one turn, then the loop is closed and
        nothing is written to ``agents/`` — the existing ``subAgent`` /
        ``parallel_research`` behaviour is unchanged.
        """
        try:
            handle = self._open_handle(
                task, name, workdir, parent,
                parent_span=parent_span, observer=observer,
                output_schema=output_schema, agent=agent,
                context_mode=context_mode, readonly=readonly,
                ephemeral=True, request=request,
                origin_session=origin_session,
                task_id=task_id, fence=fence,
            )
        except _StartError as exc:
            return SubagentResult(status="error", error=str(exc))
        try:
            return await self._run_turn(handle, task)
        finally:
            loop = handle.loop
            if loop is not None:
                try:
                    await loop.aclose()
                except Exception:  # noqa: BLE001 — release must not mask result
                    pass

    def _open_handle(
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
        run_id: str = "",
        ephemeral: bool = False,
        defer_loop: bool = False,
        request: SubagentTask | None = None,
        origin_session: str = "",
        task_id: str = "",
        fence: int = 0,
    ) -> AgentHandle:
        """Build the child's session + loop, and register it when addressable.

        The logic below is the old ``_run_in`` body, unchanged; only its tail
        differs: an addressable handle persists its session and sidecar and
        lands in ``_handles``.
        """
        target, problem = self._resolve_target(agent)
        if problem:
            raise _StartError(problem)
        ts = now_utc()
        mode = context_mode
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
        handle = AgentHandle(
            run_id=run_id,
            name=name,
            task=task,
            session=session,
            session_store=self._session_store,
            loop=None,
            workdir=workdir,
            readonly=readonly,
            agent=target.id if target is not None else agent,
            context_mode=mode,
            parent_permissions=parent.permissions if parent is not None else (),
            parent_span=parent_span,
            emit=None,
            origin_session=origin_session,
            output_schema=output_schema,
            observer=observer,
            ephemeral=ephemeral,
            request=request,
            task_id=task_id,
            fence=fence,
        )
        if not defer_loop:
            handle.loop = self._build_loop_for(handle, workdir)
            handle.ready.set()
        if not ephemeral:
            if not run_id:
                raise _StartError("an addressable sub-agent needs a run id")
            self._session_store.save(session)
            self._handles[run_id] = handle
            self._write_meta(handle)
        return handle

    def _build_loop_for(
        self, handle: AgentHandle, workdir: str | None, registry: Any | None = None
    ) -> AgentLoop:
        """The child loop, built from the handle's own facts (C-5b).

        Extracted so a resumed turn or a worktree re-attach can rebuild the
        engine without duplicating the construction rules.  ``registry`` is
        supplied when the tools must survive a resume (the file-read tracker
        inside ``write``/``edit`` lives there).
        """
        registry = registry if registry is not None else self._build_tools(workdir)
        handle.registry = registry
        engine = self._engine_factory()
        prompt = (
            PromptAssembler(self._base_instructions)
            if self._base_instructions
            else None
        )
        store: Any = _NullSessions() if handle.ephemeral else self._session_store
        return AgentLoop(
            engine,
            store,
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
            project=self._project if handle.context_mode != "task_only" else None,
            settings=self._subagent_settings(),
            guards=self._guards,
            workspace=workdir or self._workspace,
            observer=self._scoped_observer(
                handle.name, handle.parent_span, handle.observer
            ),
            # M1a ④: the parent's session layer, carried on the packet.  The
            # child's own session starts empty, so this is the only path by
            # which "deny edit *.env" reaches the work it delegates.
            session_permissions=handle.parent_permissions,
            agents=self._agents,
            # C-5b: the inbound lane for ``send_message``.  A bound method of the
            # handle's own mailbox, so the loop never sees the deque itself.
            agent_inbox=functools.partial(_drain_mailbox, handle.mailbox),
            # P2-B3: the durable notice lane belongs to the main session only;
            # a child has no human turn boundary to deliver it.
            notice_store=None,
        )

    async def _attach_loop(self, handle: AgentHandle, workdir: str | None) -> None:
        """(Re)build the child loop for ``workdir`` and mark the handle ready."""
        same_workdir = handle.workdir == workdir
        registry = handle.registry if same_workdir else None
        if handle.loop is not None:
            try:
                await handle.loop.aclose()
            except Exception:  # noqa: BLE001 — a rebuild must not fail on close
                _log.debug("child loop close failed", exc_info=True)
        handle.loop = self._build_loop_for(handle, workdir, registry)
        handle.workdir = workdir
        handle.ready.set()
        if not handle.ephemeral:
            self._write_meta(handle)

    async def _run_turn(
        self, handle: AgentHandle, text: str, request: SubagentTask | None = None
    ) -> SubagentResult:
        """Run exactly one turn on the handle's existing session.

        Turn-level cancellation waits for the inner turn to finish closing
        (``_close_turn`` must land its state before anything else touches the
        loop) — the same rule DF-2/DF-3 spells out for the next phase.

        P2-A: this is the **only** place a ``message_id`` is generated, and the
        only place the v3 ledger is written (the file name is that message id).
        Both the normal and the timeout branch go through the same envelope, so
        changed paths survive a cut.
        """
        loop = handle.loop
        if loop is None:
            # C-5b: a deferred (background/worktree) or interrupted handle may
            # not have a loop yet; build one from the session before the turn.
            await self._attach_loop(handle, handle.workdir)
            loop = handle.loop
        assert loop is not None
        if handle.stopped:
            return SubagentResult(
                status="cancelled", error="stopped", truncated=True,
                exit_reason="stopped",
            )
        scheduler = self.scheduler
        if scheduler is not None and handle.task_id:
            turn = int(getattr(handle.session, "turn_count", 0) or 0) + 1
            started = getattr(scheduler, "on_turn_start", None)
            if callable(started) and not started(
                handle.task_id, handle.fence, turn
            ):
                return SubagentResult(
                    status="cancelled", error="stale task", truncated=True,
                    exit_reason="rejected_stale",
                )
        task = request or handle.request or SubagentTask.from_objective(text)
        # R1-3: the format contract rides as a trailer on the task text —
        # the task *is* the child's user message, so no loop/engine change is
        # needed for the child to see it.
        child_task = text
        if handle.output_schema:
            # One format instruction, never two.  The old prose "uncertainty
            # protocol" is gone: nothing read its notes and the confidence it
            # collected had no consumer.
            child_task += _format_instruction(handle.output_schema)
        inner = asyncio.create_task(
            loop.process_input(handle.session, child_task)
        )
        try:
            done, _pending = await asyncio.wait(
                {inner}, timeout=self._timeout
            )
        except asyncio.CancelledError:
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            raise
        if not done:
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            result = SubagentResult(
                status="timeout",
                error=f"exceeded {self._timeout:g}s",
                workdir=handle.workdir,
                # R0-3: an outer cut is still a cut — unfinished, with a cause.
                truncated=True,
                exit_reason="outer_timeout",
                context_mode=handle.context_mode,
                # A cut must not lose what the tools already declared; the
                # ledger below carries them for the parent.
                artifacts=tuple(loop.last_artifacts),
                changed_paths=_changed_paths(loop.last_artifacts),
            )
        else:
            inner.result()  # propagate a crash exactly as a direct await did
            result = self._packet(
                loop, handle.session, handle.workdir,
                handle.context_mode, handle.output_schema,
            )
        # P2-B2: compare this run's declared changes with the recent runs before
        # the ledger is written, so the packet the parent reads and the durable
        # copy name the same warning.  A sync run has no run id; its child
        # session id is the unique key (one session = one run).
        coordinator = self._write_coordinator
        if coordinator is not None:
            key = handle.run_id or handle.session.session_id
            coordinator.register_run(
                key, result.changed_paths, handle.origin_session
            )
            conflicts = coordinator.overlap(key, result.changed_paths)
            result = replace(result, conflicts=conflicts)
        header = PacketHeader(
            message_id=uuid.uuid4().hex[:12],
            run_id=handle.run_id,
            origin_session=handle.origin_session,
        )
        ledger_path = _write_ledger(
            AgentPacket(header=header, task=task, result=result),
            self._ledger_root,
        )
        if ledger_path:
            result = replace(result, ledger_path=ledger_path)
        handle.last_status = result.status
        if not handle.ephemeral:
            self._session_store.save(handle.session)
            self._write_meta(handle)
        return result

    async def _commit_turn(
        self,
        result: SubagentResult,
        worktree_manager: Any | None,
        worktree_path: str | None,
        name: str,
    ) -> SubagentResult:
        """Commit a worktree turn and fold the outcome into ``result``.

        Takes the three facts explicitly (not a handle) because the synchronous
        path's manager is a local variable of ``_run_isolated``.  A failed
        commit becomes ``commit_error`` — never a silently empty result — and
        the ledger is refreshed so the full packet names the same facts.
        """
        if worktree_manager is None or not worktree_path:
            return result
        sha, error = await worktree_manager.commit_if_dirty(
            worktree_path, f"subagent: {name}"
        )
        if sha:
            result = replace(result, commit=sha)
        elif error:
            result = replace(result, commit_error=error)
        if sha or error:
            _refresh_ledger(result)
        return result

    @staticmethod
    def _with_handle_branch(
        handle: AgentHandle, result: SubagentResult
    ) -> SubagentResult:
        """Name the branch of a turn that actually committed (or failed to)."""
        if (
            handle.worktree_branch
            and not result.worktree_branch
            and (result.commit or result.commit_error)
        ):
            return replace(result, worktree_branch=handle.worktree_branch)
        return result

    @staticmethod
    def _record_handle_facts(handle: AgentHandle, result: SubagentResult) -> None:
        """Keep the handle's write facts in step with its latest result.

        A later turn with nothing to commit reports no commit; overwriting the
        handle's facts with that ``None`` would erase the sha the first turn
        earned and the sidecar (the only durable record) would lose it too.
        """
        if result.commit:
            handle.commit = result.commit
            handle.commit_error = None
        elif result.commit_error:
            handle.commit_error = result.commit_error
        if result.degraded:
            handle.degraded = result.degraded
        if result.worktree_branch:
            handle.worktree_branch = result.worktree_branch
        if result.base_commit:
            handle.base_commit = result.base_commit

    @staticmethod
    def _clear_runtime_markers(session: SessionData) -> None:
        """Drop markers that belong to a dead turn, never to a resumed one."""
        session.pending_approval = None
        session.pending_posture = None
        session.write_ahead = None
        session.open_turn = None
        session.pending_reminders = None
        session.inbox = []
        session.recovered_interrupted = False

    def _write_meta(self, handle: AgentHandle) -> None:
        """Write the handle's sidecar (section 7), best-effort and atomic."""
        if not handle.run_id:
            return
        payload: dict[str, Any] = {
            "version": 1,
            "runId": handle.run_id,
            "name": handle.name,
            "task": handle.task,
            "sessionId": handle.session.session_id,
            "parentSessionId": handle.origin_session,
            "workdir": handle.workdir,
            # The workspace a cross-process resume falls back to when the
            # worktree directory is gone.
            "workspace": handle.workdir or self._workspace,
            "readonly": handle.readonly,
            "agent": handle.agent,
            "contextMode": handle.context_mode,
            "permissions": [
                rule_to_dict(rule) for rule in handle.parent_permissions
            ],
            "status": handle.status,
            "lastStatus": handle.last_status,
            # P2-A: the input packet, the shape contract and the write facts.
            # A resume rebuilds the task/schema from here; close writes them
            # *before* removing the worktree so the sha is never lost.
            "taskPacket": (
                handle.request.to_dict() if handle.request is not None else None
            ),
            "outputSchema": handle.output_schema,
            "worktreeBranch": handle.worktree_branch,
            "commit": handle.commit,
            "baseCommit": handle.base_commit,
            "commitError": handle.commit_error,
            "degraded": handle.degraded,
            "createdAt": handle.session.created_time,
            "updatedAt": now_utc(),
        }
        path = self._meta_root / f"{handle.run_id}.json"
        try:
            self._meta_root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(to_json_pretty(payload), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            _log.debug("sub-agent meta write failed for %s", handle.run_id,
                       exc_info=True)

    def _load_meta(self, run_id: str) -> dict[str, Any] | None:
        """Read a handle's sidecar (unused until the resume phase, C-5b-4)."""
        if not run_id:
            return None
        # A sidecar path is a filesystem read keyed by the id: reject anything
        # that is not the hex/alnum shape a run id has (no ``../`` traversal).
        if not run_id.isascii() or not run_id.isalnum():
            return None
        path = self._meta_root / f"{run_id}.json"
        if not path.is_file():
            return None
        try:
            data = from_json_file(path)
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _subagent_settings(self) -> AIConfig:
        """Settings for the sub-turn: its own wall-clock ceiling (R2a).

        ``subagent_max_tool_iterations`` decides the child's iteration cap:
        ``0`` (default) means no cap, matching Codex/opencode/dsh/Claude Code.
        The child used to inherit the parent's ``max_tool_iterations``, so a
        parent configured at 10 cut a real child task mid-way (measured).
        """
        configured = float(getattr(self._settings, "turn_timeout_seconds", 0.0) or 0.0)
        budget = self._timeout * _INNER_BUDGET_RATIO
        if configured > 0:
            budget = min(budget, configured)
        child_iterations = int(
            getattr(self._settings, "subagent_max_tool_iterations", 0) or 0
        )
        # C3 off: the parent is blocked on this child, so a continuation buys
        # the parent nothing but latency — and it multiplies the child's
        # iterations by (1 + max_auto_continuations).  Measured with an open
        # todo item and max_tool_iterations=3: 12 model calls instead of 4.
        return self._settings.model_copy(
            update={
                "turn_timeout_seconds": max(budget, 0.01),
                "max_auto_continuations": 0,
                # 0 -> -1: "no cap", the sentinel TurnPolicyConfig understands.
                "max_tool_iterations": (
                    child_iterations if child_iterations > 0 else -1
                ),
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
            changed_paths=_changed_paths(loop.last_artifacts),
        )
        return result


def _changed_paths(artifacts: list[Any]) -> tuple[str, ...]:
    """Declared file changes: ``kind == "file"`` paths, deduped, order kept."""
    changed: list[str] = []
    for artifact in artifacts:
        if artifact.kind != "file" or not artifact.path:
            continue
        if artifact.path not in changed:
            changed.append(artifact.path)
    return tuple(changed)


def _drain_mailbox(mailbox: deque[str]) -> list[str]:
    """Take every queued message for one child; the loop never sees the deque.

    ``deque.popleft`` is the drain primitive; wrapping it keeps the mutable
    queue out of the loop's hands (the loop gets a ``Callable[[], list[str]]``).
    """
    lines: list[str] = []
    while mailbox:
        lines.append(mailbox.popleft())
    return lines


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _write_ledger(packet: AgentPacket, root: Path | None) -> str | None:
    """Write the full v3 packet; return its path (best-effort, ``None`` on fail).

    This is the **only** ledger writer.  The file name is the packet's
    ``message_id``, so every projection can derive it without a second field.
    """
    if root is None:
        return None
    try:
        target_dir = root / "transcripts" / "subagents"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{packet.header.message_id}.json"
        path.write_text(to_json_pretty(packet.to_dict()), encoding="utf-8")
    except OSError:
        return None  # best-effort: no ledger, but the packet still flows
    return str(path)


def _refresh_ledger(result: SubagentResult) -> None:
    """Merge the post-commit facts into the ledger written by ``_run_turn``.

    The ledger is written inside ``_run_turn`` before the commit exists; this
    read-update-write keeps the header and ``task`` and only overwrites the
    result keys, so the "full packet" file names the branch instead of a
    removed worktree (and carries ``commit_error`` when the commit failed).
    """
    if not result.ledger_path:
        return
    path = Path(result.ledger_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
        payload.update(result.to_dict())
        path.write_text(to_json_pretty(payload), encoding="utf-8")
    except (OSError, ValueError):
        pass  # best-effort: the packet itself already carries the facts


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


def _summary_of(result: SubagentResult) -> str:
    """The capped one-line summary shared by the row, the notice and the event."""
    summary = (result.conclusion or result.error or "").strip().replace("\n", " ")
    # r25/v2/F5: a background schema call used to lose its verdict here --
    # the model saw prose and a ledger pointer only.  One short marker is
    # enough; the facts stay in the ledger.
    if len(summary) > _SUMMARY_TAIL:
        summary = summary[:_SUMMARY_TAIL] + "…"
    # Append after the cut: a long conclusion must not eat the verdict.
    if result.schema_valid is False:
        summary = f"{summary}（结构校验未过）".strip()
    return summary


def _details_projection(result: SubagentResult) -> dict[str, Any]:
    """The event's crop of one settlement (never conclusion/task).

    ``messageId`` is derived from the ledger file name — the header field is
    already on disk, so the event needs no new packet field.
    """
    message_id = Path(result.ledger_path).stem if result.ledger_path else None
    return {
        "v": SUBAGENT_PACKET_VERSION,
        "messageId": message_id,
        "status": result.status,
        "truncated": result.truncated,
        "exit_reason": result.exit_reason,
        "facts": dict(result.facts) if result.facts is not None else None,
        "schema_valid": result.schema_valid,
        "changed_paths": list(result.changed_paths),
        "worktree_branch": result.worktree_branch,
        "commit": result.commit,
        "commit_error": result.commit_error,
        "degraded": result.degraded,
        "conflicts": [
            {**entry, "paths": list(entry.get("paths") or ())}
            for entry in result.conflicts
        ],
        "usage": dict(result.usage) if result.usage is not None else None,
        "ledger_path": result.ledger_path,
    }


def _notice_text(run_id: str, name: str, result: SubagentResult) -> str:
    """One harness line for a background run that settled.

    It is written for the model's next request, and it must survive
    ``SteeringQueue.put``'s two rules: never empty, never starting with ``/``
    (a command is the caller's to place).  A fixed marker prefix buys both, and
    the ledger pointer is what makes a 200-character summary enough -- the file
    is inside the workspace, so the model can read the whole packet.

    P2-A: the line is a projection of ``result`` — status, schema verdict,
    facts summary, changed count, branch/commit, commit failure and isolation
    degradation.  Without the last two a lost write or a silent fallback into
    the main workspace looked like a clean completion.
    """
    word = _NOTICE_STATUS.get(result.status, result.status)
    line = f"[子任务{word}] {name}（{run_id[:12]}）"
    body = " ".join(str(_summary_of(result) or "").split())
    if body:
        line += f"：{body}"
    if result.facts:
        parts = [
            f"{key}={str(value)[:60]}"
            for key, value in list(result.facts.items())[:3]
        ]
        if parts:
            line += f" · facts: {', '.join(parts)}"
    if result.changed_paths:
        shown = ", ".join(result.changed_paths[:3])
        more = f" 等 {len(result.changed_paths)} 个" if len(result.changed_paths) > 3 else ""
        line += f" · 改动 {len(result.changed_paths)} 个文件: {shown}{more}"
    if result.worktree_branch:
        line += f" · 分支 {result.worktree_branch}（未合并）"
    if result.commit:
        line += f" · 提交 {result.commit[:7]}"
    if result.commit_error:
        line += f" · 提交失败：{result.commit_error}"
    if result.degraded:
        line += f" · 降级：未隔离（{result.degraded}），已在主工作区执行"
    for conflict in result.conflicts:
        key = str(conflict.get("key") or "")[:12]
        paths = ",".join(str(path) for path in conflict.get("paths") or ())
        line += f" · ⚠ 与 {key} 改动重叠: {paths}（未合并）"
    if result.ledger_path:
        line += f" · 完整回包 {result.ledger_path}"
    return line
