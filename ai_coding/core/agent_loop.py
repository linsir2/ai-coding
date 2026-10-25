"""The self-owned ReAct loop (R1a-4, re-shaped in R5, slimmed in R7a).

The loop is *ours*: it drives the iteration, decides when a turn is over, gates
the stop on the task list, executes tools through the dispatcher, and checkpoints
the session between iterations.  The engine only ever performs one model call.

The body is one turn in three moves, and each move is a unit you can point at:

    _begin_turn()   -> bind observer/trace, count the turn, reset the guards,
                       ask turn.start hooks            (core/turn_hooks.py)
    for each iteration:
        ask      -> TurnPolicy, fed by TurnBooks.snapshot()      (core/turn_books.py)
        assemble -> _assemble(): compress + instructions + the four volatile tail
                    blocks (memory / task / hook / digest) + report_context
        model    -> _model_step(): one step, timed and reported
        act      -> _run_tools(): dispatch, write back, checkpoint
        (no tool calls) -> TurnPolicy.decide_after_step(): finish, or nudge
    _finish_turn()  -> TurnCloser.close(): handoff + digest + persist, harvest,
                       turn.end hooks, then the text the user sees
                       (core/turn_close.py)

Four things are deliberately asymmetric, and the R5/R7 notes say why:

* the ``<task-state>``/memory/hook blocks are **volatile** (regenerated every
  request, never persisted) while the ``[task-gate]`` nudge is **persisted** — it
  is a real instruction that changed the model's behaviour, so replaying the
  transcript without it would break the conversation's meaning
  (docs/r1a-4-analysis.md D1);
* memory, compaction, tracing and hooks are **best-effort**: they degrade and
  *say so* (a ``Degraded`` event) rather than ending a turn, while a model-call
  failure does end it (docs/r2-analysis.md 7.3);
* observation is **write-only** — it never changes a request, a message or a
  decision (docs/r4-analysis.md P1);
* the volatile tail is the *only* region allowed to differ between iterations,
  which is what keeps the provider's prefix cache alive (docs/r3-analysis.md 1.13).

What stayed here on purpose: the iteration, the model call and tool dispatch.  The
counters, the closing half and the hook plumbing moved out in R7a because R6 had
grown them back inside a single 228-line method (docs/r7-analysis.md 3.1).
A loop you cannot read is not "ours" any more (docs/r5-analysis.md 2.2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.attachments import load_image
from ai_coding.config.models import AIConfig
from ai_coding.core import interventions
from ai_coding.core.acceptance import verify_items
from ai_coding.core.compaction import Compactor
from ai_coding.core.dispatch import DispatchOutcome, ToolDispatcher
from ai_coding.core.events import (
    AgentSwitched,
    AssistantMessage,
    InputSteered,
    IterationStarted,
    LedgerUpdated,
    LoopEvent,
    LoopObserver,
    NullObserver,
    ProviderFallback,
    ProviderRetried,
    SearchStalled,
    TaskUpdated,
    ToolSurfaceWarning,
)
from ai_coding.core.guard import GuardChain, TurnView
from ai_coding.core.hooks import HookPoint
from ai_coding.core.memory_turn import TurnMemory
from ai_coding.core.observers import Braid, SafeObserver
from ai_coding.core.plan_reminders import PLAN_ENTER_REMINDER, reminder_for
from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.request_builder import (
    tool_context,
)
from ai_coding.core.static_check import run_static_checks
from ai_coding.core.trace import short_json
from ai_coding.core.turn_books import TurnBooks, TurnFrame
from ai_coding.core.turn_close import (
    TurnCloser,
    aborted_outcome,
    failed_outcome,
    stopped_outcome,
    terminal_outcome,
)
from ai_coding.core.turn_hooks import TurnHooks
from ai_coding.core.turn_policy import (
    PLAN_APPROVAL_WITHDRAWN_NOTE,
    PLAN_DISMISSED_NOTE,
    PLAN_RESEARCH_NOTE,
    TurnAction,
    TurnDecision,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.core.turn_report import TurnReporter
from ai_coding.core.undo_store import UndoFileStore
from ai_coding.domain.acceptance import acceptance_states, pending_items
from ai_coding.domain.agents import AgentCatalog
from ai_coding.domain.ledger import ledger_signature
from ai_coding.domain.loop import (
    EmptyStepError,
    StepResult,
    ThinkingOnlyStepError,
    TurnOutcome,
    TurnStatus,
)
from ai_coding.domain.message import (
    IMAGE_MIME_BY_SUFFIX,
    ChatMessage,
    ImageRef,
    ToolArtifact,
)
from ai_coding.domain.model_config import ModelConfig
from ai_coding.domain.run import assistant_message, user_message
from ai_coding.domain.session import SessionData
from ai_coding.domain.stance import PostureResult
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import TurnDigest
from ai_coding.infra.deadline import (
    Deadline,
    DeadlineExpired,
    remaining_of,
    run_with_deadline,
)
from ai_coding.infra.time_utils import now_utc
from ai_coding.infra.vcs import git_branch
from ai_coding.security.approval import ApprovalCallback
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.permissions import Rule, posture_of, rules_from_dicts
from ai_coding.service.session_service import SessionService
from ai_coding.skills.catalog import build_catalog, superseded_note
from ai_coding.skills.policy import denied_skill_ids
from ai_coding.tools.base import BaseTool, TurnConclusion
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.schema import tool_schema, tool_schemas, tool_schemas_from_tools

if TYPE_CHECKING:
    from ai_coding.ai.prompt import PromptAssembler
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.core.project_instructions import ProjectInstructionsLoader
    from ai_coding.memory.service import MemoryService
    from ai_coding.skills.registry import SkillRegistry

DeltaSink = Callable[[str], None]



_log = logging.getLogger(__name__)

#: G5-1: the verdict string from ``FailureKind.CONTEXT_OVERFLOW``.  Spelled out
#: rather than imported because the loop must not depend on which engine
#: implementation produced it -- the same reason ``ProviderRetried.kind`` is a
#: plain string on the event.
_CONTEXT_OVERFLOW = "context_overflow"

#: G8-2: the decision reason that means "the iteration budget is what stopped
#: this turn".  Matched by value because the policy states reasons as text and
#: they are part of its observable contract (tests assert on them).
_ITERATION_BUDGET = "iteration budget exhausted"

#: G8-2: what the model is asked when the harness would otherwise write the
#: report.  It asks for a summary, and says not to start anything: an unanswered
#: "what should I do next" would just spend the round on more work.
_CLOSING_PROMPT = (
    "本轮步数预算已用完，请用一段话收尾：说明已经完成了什么、还差什么、"
    "下一步该做什么。不要再调用任何工具。"
)

#: 13 §2.5 (B): the synthetic line that resumes a broken stream.  Persisted like
#: a gate nudge -- it changed what the model did next (opencode parity).
_STREAM_CONTINUE = (
    "[stream-interrupted] 上一条回复在传输中中断，从未完成处继续，"
    "不要重复已完成的内容。"
)

#: How long a reactive compaction may take when the turn has no deadline left
#: to divide.  Matches the floor ``compact_now`` already applies to ``/compact``.
_REACTIVE_COMPACT_SECONDS = 60.0

#: Rescue compactions one turn may spend on provider context-overflow
#: rejections (11-spec §7.3).  The old one-pass wall predated checkpoint
#: merging; a second pass now UPDATES the checkpoint instead of shredding a
#: summary of a summary, and a pass that removes nothing still ends the turn
#: honestly (the ``removed == 0`` guard in the catch block).
_MAX_OVERFLOW_COMPACTS = 2


def _tools_by_origin(registry: ToolRegistry) -> dict[str, int]:
    """Serialised schema chars per origin, biggest first (R7e-3).

    Per *tool* rather than per group, so the numbers add up to the same schemas the
    request carries (the whole-array text differs only by its brackets and commas).
    """
    sizes: dict[str, int] = {}
    for tool in registry.all_tools:
        sizes[tool.origin] = sizes.get(tool.origin, 0) + len(short_json(tool_schema(tool)))
    return dict(sorted(sizes.items(), key=lambda item: (-item[1], item[0])))


def _on_plan_approved(session: SessionData, books: TurnBooks) -> None:
    """What a plan approval changes, in one place.

    Two things, both about the same moment: the session's ladder windows
    restart (R10 -- a new episode must not ride the old one's numbers), and
    the books learn that this turn approved a plan (W2 -- the approval
    belongs to this turn, so a turn that does not end cleanly takes it back
    at its close).
    """
    session.intervention_state = None
    books.note_plan_approved()


@dataclass(frozen=True)
class _Staged:
    """What a staged revert has to put back, beyond the messages.

    ``turn_digests`` is the reason this exists: undo trims the digest lines of
    the turns it removes, and trimming destroys them.  A /redo with only the
    message tail would bring the turns back with their summaries missing, so
    the working-memory ring would never mention work the transcript shows.
    """

    turns: int
    turn_count: int
    digests: tuple[TurnDigest, ...]


class AgentLoop:
    """Owns a conversation turn: iteration, stop policy, tools, persistence."""

    def __init__(
        self,
        engine: ChatEngine,
        sessions: SessionService,
        tools: ToolRegistry,
        *,
        gate: PermissionGate | None = None,
        approval: ApprovalCallback | None = None,
        prompt: PromptAssembler | None = None,
        # M2: per-step MCP catalog text (``mcp_call``'s other half).  The
        # composition root hands it in, from the same manager the tool closes
        # over, so the text and the callable surface cannot drift apart.
        catalog_builder: Callable[[], str] | None = None,
        context: ContextManager | None = None,
        memory: MemoryService | None = None,
        skills: SkillRegistry | None = None,
        project: ProjectInstructionsLoader | None = None,
        settings: AIConfig | None = None,
        models: Mapping[str, ModelConfig] | None = None,
        default_model: str = "",
        observer: LoopObserver | None = None,
        workspace: str = ".",
        trace: Any | None = None,
        guards: GuardChain | None = None,
        hooks: Any | None = None,
        # P0-2: the harness lane -- a background run settles long after the turn
        # that started it, and its event reaches the operator but not the model.
        notices: Any | None = None,
        jobs: Any | None = None,
        cancel: asyncio.Event | None = None,
        snapshots: Any | None = None,
        # B/D5-c: the roots ``tools.extra_workspace_roots`` declared, already
        # expanded by the composition root.  Passed in because ``settings`` here
        # is ``AIConfig`` and the list lives on ``ToolsConfig``; the loop is not
        # the place that would go looking for it.
        extra_workspace_roots: Sequence[str] = (),
        # M1a: the parent's session layer, handed to a sub-agent's loop.  A
        # child has its own fresh session, so without this the parent's
        # rules would be invisible to it (④).  Empty for a top-level loop:
        # its rules come from its own session.
        session_permissions: Sequence[Rule] = (),
        # M1b: the agent table.  ``None`` means "no agent layer at all" --
        # every existing construction site (tests, the bare REPL) keeps
        # answering exactly as it did; only the composition root passes one.
        agents: AgentCatalog | None = None,
    ) -> None:
        self.engine = engine
        self.sessions = sessions
        self.tools = tools
        self.prompt = prompt
        self.catalog_builder = catalog_builder
        self.context = context
        self.memory = memory
        self.skills = skills
        self.project = project
        # r17/T1: pre-turn workspace snapshots (``core/snapshots.SnapshotStore``).
        # ``None`` (every sub-agent, and every caller that did not wire one)
        # means undo has no files to restore — the history half still works.
        self.snapshots = snapshots
        # r18/F5: one loop serves every session the TUI switches between, so
        # the staged undo state is keyed by session rather than held in one
        # slot -- otherwise /redo in one session replays another's turn.
        # Read it through ``last_undo_for`` rather than reaching in here.
        self.last_undo: dict[str, dict[str, Any]] = {}
        # The messages undo removed, held for /redo, per session.  In-process
        # only: a restart loses redo, which is the honest cost of not writing it
        # to the session file.
        self._undo_tail: dict[str, list[ChatMessage]] = {}
        # r20/F2: one staged revert per session rather than one per keystroke.
        # ``_Staged`` carries what undo takes away and cannot get back on its own
        # -- the counter and the digests it trims -- so that a later /redo has
        # something to restore them from.
        self._undo_state: dict[str, _Staged] = {}
        # The content of files a turn created, read before undo removed them.
        # A git stash holds tracked files only, so this is the only copy that
        # exists (r20/F9).
        self._undo_files: dict[str, dict[str, bytes]] = {}
        self.settings = settings or AIConfig()
        # The model table lives on ``AppConfig``, not on ``AIConfig``: it is a
        # catalogue of connections, not a behaviour knob, and ``AIConfig`` is
        # shared by every entry point.  Without it ``/model`` could only ever
        # report "unknown model" (``set_model`` below is the only reader).
        self.models: dict[str, ModelConfig] = dict(models or {})
        # Remembered separately from ``engine.model_config`` so ``/model`` can
        # still mark the configured default after the user switched away.
        self._current_instruction: ChatMessage | None = None
        self.default_model = default_model
        self.workspace = workspace
        # W2/R02: the operator's declared roots, kept here because three
        # consumers ask the same reachability question and must get the same
        # answer -- the plan gates (``core/plan_runner.py``) and the tool
        # context that carries them into ``present_plan``.  The assembler gets
        # its own copy below; this one is the source both read.
        self.extra_workspace_roots: tuple[str, ...] = tuple(extra_workspace_roots)
        self._session_permissions: tuple[Rule, ...] = tuple(session_permissions)
        self.agents = agents
        # M1b: the parsed rule layer per agent id.  The catalog holds raw
        # rows (``domain`` may not import ``security``), so the parse happens
        # here, once per agent rather than once per call.
        self._agent_rule_cache: dict[str, tuple[Rule, ...]] = {}
        self._agent_rule_errors: dict[str, tuple[str, ...]] = {}
        # P4 (补充式方案 06): where /undo keeps the bytes of files the turn
        # created, so /redo survives a restart (a stash never held them).
        self._undo_files_store = UndoFileStore(
            Path(workspace) / ".ai-coding" / "undo"
        )
        self._base_observer: LoopObserver = observer or NullObserver()
        self.observer: LoopObserver = SafeObserver(self._base_observer)
        # R4a: an optional trace recorder.  It arrives as part of the observer
        # braid, and the loop only re-binds it to the current session (the CLI
        # builds one loop for a whole REPL, so the session can change under it).
        self.trace = trace
        # R4c: user hooks.  ``turn.start`` may inject context, which lands in the
        # volatile tail (never the system prompt, never persisted) so the cache
        # discipline from R3 still holds (docs/r4-analysis.md 6.4).  R7a: the two
        # calls a turn makes live in their own unit.
        self.hooks = hooks
        self.turn_hooks = TurnHooks(hooks, workspace=self.workspace)
        # P0-2: same queue shape, different speaker.  None = today's behaviour,
        # and the loop then never looks at it.
        self.notices = notices
        # D-1: background jobs.  Kept beside notices because the two travel
        # together -- a job that finishes writes into that same queue.
        self.jobs = jobs
        # R8c: what the Esc key sets.  The loop clears it before every batch so one
        # key press drops one batch, never the rest of the turn (D-7).
        self.cancel = cancel
        # Task injection + stop policy live in their own pure unit (R2c).
        self.policy = TurnPolicy(TurnPolicyConfig.from_ai_config(self.settings))
        # M1: the engine owns the retry loop but has no observer of its own, so the
        # loop subscribes on its behalf and restates a retry as an event.
        # Subscribing here rather than in the TUI keeps the contract honest for
        # every consumer at once (transcript, trace recorder, REPL).  Engines
        # that never retry simply never call it.
        engine.on_retry = self._restate_engine_retry
        # M2: the same seam one level up.  A chain of engines decides when to move
        # on, but only the loop knows how to say so, so it subscribes on their
        # behalf -- otherwise the switch stays silent until the status bar
        # refreshes at the end of the command.
        engine.on_fallback = self._restate_engine_fallback
        # G1-1: "one turn at a time" was a Textual property -- an exclusive
        # worker plus a submit queue -- so it held inside one app instance and
        # nowhere else.  The REPL, a plan step and any future front end had only
        # the convention that callers happen to await in order, and two turns that
        # interleave append their messages in whatever order they finish.  Holding
        # the rule here makes it the loop's own; the UI keeps its own because it
        # answers a different question (whether the box should accept input).
        self._turn_lock = asyncio.Lock()
        self._tool_schemas = tool_schemas(tools)
        # N1: the tool table a request carries depends on the posture.  A
        # plan-only tool (the research exit) belongs to the planning
        # posture: outside it, the model must not see a tool whose every
        # call could only be refused.  The lists are derived per posture and
        # cached, so a request stays byte-identical (the prompt cache and
        # the provider's prefix cache depend on it).
        self.gate: PermissionGate = gate or PermissionGate()
        self._surface_cache: dict[
            tuple[str, bool, tuple[Rule, ...], tuple[Rule, ...]],
            list[dict[str, Any]],
        ] = {}
        # R4a: the tool schemas are part of every prompt, so their size belongs in
        # the attribution (docs/r4-analysis.md 4.3).
        self._tools_text = short_json(self._tool_schemas)
        self._tools_chars = len(self._tools_text)
        # R7e-3: the tool surface is static, so its breakdown is computed once and
        # the warning costs nothing per turn.
        self._tools_by_origin = _tools_by_origin(tools)
        self._tools_warned = False
        self.dispatcher = ToolDispatcher(
            tools,
            self.gate,
            approval,
            max_parallel=self.settings.max_concurrent_tools,
            observer=self.observer,
            guards=guards,
            hooks=hooks,
            # R7c: an approval prompt is a wait like any other — the turn clock
            # bounds it, and a configured ceiling bounds it even without a clock.
            approval_timeout=float(
                getattr(self.settings, "approval_timeout_seconds", 0.0) or 0.0
            )
            or None,
            cancel=cancel,
        )
        # R5: everything this turn says about itself (events, ledger, degradation)
        # lives in one unit, so the loop body only has to choreograph.
        self.reporter = TurnReporter(tools_text=self._tools_text, emit=self._emit)
        # R7a: what a turn leaves behind (write-back, checkpoint, handoff, digest).
        self.closer = TurnCloser(sessions, self.reporter, self.settings)
        # R5: the memory-facing half of a turn (block / harvest / status).
        self.turn_memory = TurnMemory(memory, emit=self._emit, save=self.closer.save)
        #: 2026-10-17: the fire-and-forget harvest chain.  The harvest no longer
        #: holds the turn's future open (it used to: 0-22s measured, one
        #: extractor call); each finished turn chains its harvest behind the
        #: previous one so the extractor never sees a delta twice.
        self._harvest_task: asyncio.Task[None] | None = None
        # R5: history compaction (budget-driven for turns, forced for /compact).
        self.compactor = Compactor(context, emit=self._emit)
        # R7a: one request per iteration, assembled in its own unit (the
        # volatile blocks, in their declared order).
        from ai_coding.core.sop_router import ToolObserver
        self._tool_observer = ToolObserver([])  # observations list, updated per turn

        self.assembler = RequestAssembler(
            compactor=self.compactor,
            policy=self.policy,
            # R01: the planning round and plan steps swap ``loop.policy`` for
            # their own ceiling; the assembler must read the live object, not
            # the one it was built with (F-51).
            policy_provider=lambda: self.policy,
            memory=self.turn_memory,
            reporter=self.reporter,
            build_instructions=self._build_instructions,
            catalog_builder=self.catalog_builder,
            settings=self.settings,
            emit=self._emit,
            skills=self.skills,
            workspace=workspace,
            extra_workspace_roots=extra_workspace_roots,
        )

    def set_model(self, name: str) -> None:
        """Switch to a different model.

        Args:
            name: The model name from settings.models

        Raises:
            ValueError: If the model name is not in settings.models
        """
        if name not in self.models:
            available = ", ".join(sorted(self.models)) or "none configured"
            raise ValueError(f"Unknown model: {name} (available: {available})")
        self.engine.model_config = self.models[name]

    # ---- posture (18 §10.2) ------------------------------------------------

    @property
    def turn_running(self) -> bool:
        """True while a turn (or a resume) holds the single-turn lock."""
        return self._turn_lock.locked()

    def request_posture(
        self,
        session: SessionData,
        *,
        agent: str | None = None,
        readonly: bool | None = None,
        reason: str = "",
        observer: LoopObserver | None = None,
    ) -> PostureResult:
        """Apply a posture flip now, or queue it for the running turn's close.

        The four answers are ``PostureResult`` — see that type for why a bool
        could not say the things an operator needs to know.  The requested
        axes that are ``None`` mean "leave this one alone": ``/readonly``
        sends only ``readonly``, and a queued flip must still record a
        resolved agent rather than ``None``.

        ``observer`` is how a switch made *outside* a turn reaches the
        frontend: ``self.observer`` is whatever the last turn bound, and
        before the first turn it is the construction-time ``NullObserver``.
        The command layer already holds the bridge, so it passes it in.

        No ``await`` between the check and the write, so a turn cannot start
        inside the gap.
        """
        target = (
            agent if agent is not None else session.agent,
            bool(readonly) if readonly is not None else session.readonly,
        )
        current = (session.agent, session.readonly)
        pending = session.pending_posture

        if target == current:
            # The request is "be where we already are": it either withdraws a
            # queued change or there was nothing to do.
            if pending:
                self._clear_pending_posture(session)
                return PostureResult.CANCELLED
            return PostureResult.NOOP

        if self.turn_running:
            if pending and self._pending_target(session, pending) == target:
                return PostureResult.QUEUED
            queued = {"agent": target[0], "readonly": target[1], "reason": reason}
            session.pending_posture = queued
            self.sessions.mark_pending_posture(session.session_id, queued)
            return PostureResult.QUEUED

        self.commit_posture(
            session,
            agent=target[0],
            readonly=target[1],
            reason=reason,
            observer=observer,
        )
        return PostureResult.COMMITTED

    def commit_posture(
        self,
        session: SessionData,
        *,
        agent: str | None = None,
        readonly: bool | None = None,
        reason: str = "",
        observer: LoopObserver | None = None,
    ) -> None:
        """The one place a posture lands (M1b T10).

        Every route goes through here — the immediate request, a queued flip
        drained at a turn boundary, ``/plan run``'s finally, and the boot
        reset — so the field write, the history row, the event and the
        sidecar removal cannot drift apart.
        """
        target_agent = agent if agent is not None else session.agent
        target_readonly = bool(readonly) if readonly is not None else session.readonly
        previous = session.agent
        session.agent = target_agent
        session.readonly = target_readonly
        if target_agent != previous:
            ts = now_utc()
            session.agent_history.append(
                {"from": previous, "to": target_agent, "ts": ts, "reason": reason}
            )
            event = AgentSwitched(
                from_agent=previous,
                to_agent=target_agent,
                reason=reason,
                ts=ts,
            )
            sink = observer if observer is not None else self.observer
            try:
                sink.on_event(event)
            except Exception:  # noqa: BLE001 — observation must not lose the switch
                _log.debug("AgentSwitched observer failed", exc_info=True)
            # M2 T3: the posture changed, so the model has to be told.  The
            # reminder is *queued*, not delivered: a turn may be running and
            # its request is already assembled, and a reminder that arrives
            # mid-turn would change a request the model has already seen.
            text = reminder_for(previous, target_agent)
            if text is not None:
                self._queue_reminder(session, text)
        self._clear_pending_posture(session)

    def _queue_reminder(self, session: SessionData, text: str) -> None:
        """Queue a plan reminder for the next turn boundary.

        Durable like the posture flip it belongs to: the state "the posture
        landed, the reminder has not" is real, and a crash in it would leave
        the model in a posture it was never told about.
        """
        kind = "enter" if text == PLAN_ENTER_REMINDER else "leave"
        queued = list(session.pending_reminders or ())
        queued.append({"kind": kind, "text": text})
        session.pending_reminders = queued
        self.sessions.mark_pending_reminder(session.session_id, queued)

    def _clear_pending_posture(self, session: SessionData) -> None:
        """Forget the queued flip, in memory and on disk."""
        session.pending_posture = None
        self.sessions.mark_pending_posture(session.session_id, None)

    def _apply_pending_reminders(self, session: SessionData) -> None:
        """Turn the queued reminders into synthetic messages (M2 T3).

        The boundary is the turn's close: the messages land in the history
        and the close's own save carries them to disk, so a reminder costs
        no new save point.  They are **synthetic** — the harness talking in
        the user's shape — so the readers that answer "what did the human
        say" skip them without knowing what a reminder is.
        """
        queued = list(session.pending_reminders or ())
        if not queued:
            return
        ts = now_utc()
        for row in queued:
            text = str(row.get("text") or "")
            if not text:
                continue
            session.add_message(user_message(text, ts, synthetic=True))
        session.pending_reminders = None
        self.sessions.mark_pending_reminder(session.session_id, None)

    @staticmethod
    def _pending_target(
        session: SessionData, pending: dict[str, Any]
    ) -> tuple[str, bool]:
        """Where a queued flip would land, with the unset axis resolved."""
        agent = pending.get("agent")
        readonly = pending.get("readonly")
        return (
            str(agent) if agent is not None else session.agent,
            bool(readonly) if readonly is not None else session.readonly,
        )

    def _apply_pending_posture(self, session: SessionData) -> None:
        """Land a flip queued while a turn ran (18 §10.2).

        Called at the turn's close (before ``TurnCloser.close``, so the close's
        own save carries it to disk with zero new save points) and at the next
        turn's entry (the harvest-window fallback: a flip queued after the
        close ran but before the lock released).
        """
        pending = session.pending_posture
        if not pending:
            return
        agent, readonly = self._pending_target(session, pending)
        self.commit_posture(
            session,
            agent=agent,
            readonly=readonly,
            reason=str(pending.get("reason") or ""),
        )

    # ---- the turn ---------------------------------------------------------

    async def process_input(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
        images: tuple[ImageRef, ...] = (),
        *,
        synthetic: bool = False,
    ) -> str:
        """Run one user turn to completion and return the final assistant text.

        Serialised per loop (G1-1).  A second caller waits rather than failing,
        which is the semantics the TUI already had when it queued submitted
        lines; rejecting instead would leave the front end queueing something the
        loop then refuses.

        ``synthetic`` says the input is the harness's, not the human's (M2
        T3, 16 §6).  The planning round is such an input: it is a task the
        harness sets, and marking it is what lets the readers that answer
        "what did the human say" stop guessing from a prefix.
        """
        async with self._turn_lock:
            return await self._run_turn(
                session, user_input, on_delta, observer, images, synthetic=synthetic
            )

    async def resume_turn(
        self,
        session: SessionData,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
    ) -> str:
        """Continue the interrupted turn (08): the history already ends at the
        last completed tool result (the crash's batch never checkpointed, and
        the approval wait only wrote the marker), so the model walks on from
        there -- no duplicate user message, no counter bump.  A WARN call met
        again re-asks through the *live* approval provider by itself, which is
        why resume needs no approval-reconstruction machinery.

        Refuses a session with nothing open: that would be a model turn over
        stale history with an empty prompt.
        """
        if session.open_turn is None:
            raise ValueError("nothing to resume")
        async with self._turn_lock:
            # Q3 (08): the dead turn's question dies with it.  A live turn that
            # meets the call again writes a fresh marker; carrying the stale one
            # along would report a question nobody is asking any more.
            pending = session.pending_approval
            session.pending_approval = None
            self.sessions.mark_pending_approval(session.session_id, None)
            if isinstance(pending, dict) and pending.get("toolName"):
                # 08/addendum (tmux proved it): the resumed turn may walk a
                # different path and never re-ask -- the dead plan would then
                # vanish silently.  Leave its question in the history, in the
                # open, so the model explicitly re-decides it.  Synthetic like
                # the steering lines: the harness speaking, not the human.
                params = pending.get("params")
                target = f" {short_json(params)[:80]}" if params else ""
                session.add_message(
                    user_message(
                        "[resume] 上一步在等审批："
                        f"{pending.get('toolName')}{target}（已中断）；"
                        "如仍需要，重新发起",
                        now_utc(),
                        synthetic=True,
                    )
                )
                # M1: the marker above was deleted durably, but the note is
                # still memory-only -- a crash before the turn's first
                # checkpoint would lose the pending question with no trace.
                # Persist it before the turn runs.  One save on an exceptional
                # path, so the per-turn save rhythm the golden tests pin is
                # untouched.
                self.closer.save(session)
            return await self._run_turn(
                session, "", on_delta, observer, (), append_user=False
            )

    async def _run_turn(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
        images: tuple[ImageRef, ...] = (),
        *,
        append_user: bool = True,
        synthetic: bool = False,
    ) -> str:
        """One turn, ungated.  The body below is ``process_input`` unchanged:
        splitting it out keeps the re-entry guard a two-line diff instead of a
        re-indent of the whole turn."""
        # 18 §10.2: harvest-window fallback — a flip queued after the close ran
        # (but before the lock released) lands here, before anything reads it.
        self._apply_pending_posture(session)
        # M2 T3: the same for a reminder queued after the close ran.
        self._apply_pending_reminders(session)
        await self._capture_snapshot(session)
        # r17/T5: clipboard images plus any @path.png in the text ride the user
        # message as parts; a token is claimed once, first spelling wins.
        attached = tuple(images) + tuple(
            ref for ref in self._extract_image_mentions(user_input)
            if ref.path not in {existing.path for existing in images}
        )
        frame = await self._begin_turn(
            session,
            user_input,
            observer,
            attached,
            append_user=append_user,
            synthetic=synthetic,
        )
        task = frame.task
        # R7a: everything the turn counts about itself lives in one object, so a
        # new rule is a method on the books rather than a tenth local variable.
        books = TurnBooks()
        # N1: the turn's two exits are mutually exclusive, and the holder is
        # what they read to stay so.  It belongs to the turn, not to the task:
        # a conclusion is a fact about one turn, and the task outlives it.
        conclusion = TurnConclusion()
        # R4a: the previous request's canonical form, which answers "did the prefix
        # move" (the ledger it compares against lives on the reporter).
        previous_request: str | None = None
        outcome = TurnOutcome(
            status=TurnStatus.MAX_ITERATIONS, reason="iteration budget exhausted"
        )
        # G5-1 revised (11-spec §7.3): up to _MAX_OVERFLOW_COMPACTS rescue
        # compactions per turn, each a fresh pass over the history.  The old
        # one-pass wall predated checkpoint merging; two passes stay honest
        # because the second UPDATES the checkpoint and a pass that removes
        # nothing still ends the turn (the removed==0 guard below).
        overflow_compacts = 0
        #: G8-2: the model's own wrap-up, if it was asked for one.  Kept off
        #: ``outcome.text`` on purpose -- see ``render_handoff``.
        closing = ""

        try:
            index = 0
            while True:
                index += 1
                budget_expired = frame.deadline is not None and frame.deadline.expired
                # R0-2: derive the open unknowns from the books before deciding.
                # One call site per round (here, not after every branch), so a
                # flat round counts exactly once.
                self._refresh_ledger(books)
                snapshot = books.snapshot(
                    iteration=index, tokens_spent=self.reporter.tokens_spent(),
                    # C3 reads the open set to decide whether the budget died
                    # with work unfinished.  The old ladder never needed it;
                    # without it the continuation never fires.
                    open_items=task.open_items(),
                    # settle evaluates every gate's window at both decision
                    # points; an acceptance window must see its real signature
                    # here too — with the default () it would compare empty vs
                    # ref and be mis-marked "helped" at the very next iteration.
                    acceptance=acceptance_states(session.acceptance),
                    continuations=session.continuations,
                )
                snapshot = replace(
                    snapshot,
                    mode=session.mode,
                    readonly=session.readonly,
                    intervention_ledger=self._settled_interventions(
                        session, task, snapshot
                    ),
                )
                decision = self.policy.decide_next_iteration(
                    snapshot, budget_expired=budget_expired,
                )
                self._note_intervention(session, decision, snapshot)
                if decision.action is TurnAction.FINISH:
                    # R0-1 (H1-01): a budget stop is not proof the work is
                    # undone.  When contracts exist and the clock still has
                    # room for one bounded check, verify before translating
                    # the stop into a verdict — a green suite turns MAX into
                    # COMPLETED instead of lying about it.
                    if (
                        decision.reason != "turn deadline exceeded"
                        and pending_items(session.acceptance)
                    ):
                        await self._verify_acceptance(session, books)
                        if session.acceptance and all(
                            item.passed is True for item in session.acceptance
                        ):
                            outcome = TurnOutcome(
                                status=TurnStatus.COMPLETED,
                                text=books.last_text,
                                iterations=books.total_iterations,
                                reason="acceptance verified (post-budget)",
                            )
                            break
                    outcome = terminal_outcome(
                        decision,
                        budget_expired=budget_expired,
                        last_text=books.last_text,
                        iterations=books.total_iterations,
                    )
                    # G8-2: the harness report is a status, not an answer.  When
                    # the iteration budget -- not the clock, and not the token
                    # ceiling -- is what stopped the turn, ask the model for a
                    # closing word so the operator gets prose about the work
                    # instead of a list of artifacts.  Bounded to once per turn,
                    # and skipped when there is no room left to answer.
                    if not closing and decision.reason == _ITERATION_BUDGET:
                        closing = await self._ask_for_closing(
                            session=session,
                            task=task,
                            books=books,
                            frame=frame,
                            previous_request=previous_request,
                            on_delta=on_delta,
                        )
                    break
                if decision.action is TurnAction.NUDGE:
                    # R6b tier 2.  Like the [task-gate] nudge, this is persisted: it
                    # is a real instruction that changed the model's behaviour, so
                    # replaying the transcript without it would break its meaning
                    # (docs/r1a-4-analysis.md D1).
                    restart = False
                    if decision.reason == "continue unfinished work":
                        # C3: count the continuation on the session (it survives
                        # across rounds) and re-arm the iteration budget below —
                        # without the reset the counter keeps climbing, the new
                        # round sees "budget exhausted" at once, and the model is
                        # never called again (the cap burns for zero work).
                        session.continuations += 1
                        restart = True
                    elif decision.intervention == "fatigue":
                        books.note_fatigue_nudge()
                    elif decision.intervention == "strategy":
                        books.note_strategy_nudge()
                    elif decision.intervention == interventions.SEARCH:
                        # B1: L2 is the rung that asks *for* the static check, so run
                        # it here and hand over the finding instead of another
                        # keyword to guess.  Once per turn -- a re-fired L2 must not
                        # start mypy again (seconds, for an answer that cannot have
                        # changed).
                        if (
                            decision.level >= interventions.L2
                            and not books.static_check_ran
                        ):
                            books.static_check_ran = True
                            block = await run_static_checks(
                                str(self.workspace or ""),
                                list(self.settings.static_check.commands),
                                timeout=self.settings.static_check.timeout_seconds,
                                max_chars=self.settings.static_check.max_chars,
                            )
                            if block:
                                decision = replace(
                                    decision,
                                    inject=(
                                        f"{decision.inject}\n\n{block}"
                                        if decision.inject
                                        else block
                                    ),
                                )
                    elif decision.reason == "plan unsubmitted":
                        # A4: the plan guard's remedy on a budget mouth has to
                        # be counted here too, exactly like on the post-step
                        # path.  A budget mouth stays true (the budget is
                        # still spent), so an uncounted nudge would be offered
                        # every round — an infinite spin instead of a turn.
                        books.note_plan_nudge()
                    session.add_message(
                        user_message(decision.inject or "", now_utc(), synthetic=True)
                    )
                    self.closer.checkpoint(session, index)
                    if restart:
                        index = 0
                    continue

                books.note_iteration(index)
                self._take_steering(session, books, index)
                self._take_notices(session, index)
                self._emit(
                    IterationStarted(
                        index=index, max_iterations=self.policy.config.max_iterations
                    )
                )
                assembled = await self.assembler.assemble(
                    session=session,
                    task=task,
                    books=books,
                    turn_number=frame.turn_number,
                    hook_context=frame.hook_context,
                    deadline=frame.deadline,
                    index=index,
                    previous_request=previous_request,
                    pinned=(self._current_instruction,) if self._current_instruction else (),
                )
                previous_request = assembled.previous_request

                # The one wait that cannot degrade: a model step either answers or
                # the turn ends (docs/r2-analysis.md §7.3).  The single exception
                # is a provider that rejects the *context*: that is recoverable
                # by compacting, and it is the only failure where the operator
                # has already paid for the whole turn and would lose all of it.
                try:
                    step = await self._model_step(
                        session=session,
                        request=assembled.request,
                        instructions=assembled.instructions,
                        deadline=frame.deadline,
                        on_delta=on_delta,
                        index=index,
                        books=books,
                    )
                except ThinkingOnlyStepError as exc:
                    # It thought and stopped (M7). One bounded chance to finish
                    # the sentence, then the turn ends with the reason — the same
                    # shape the turn policy already uses for "continue
                    # unfinished work", so the retry is counted, surfaced and
                    # reuses the path instead of inventing a second one.
                    #
                    # `length` is deliberately excluded: that is the answer
                    # running out of budget, not the model running out of
                    # things to say, and asking again with the same budget would
                    # reproduce the truncation. Anthropic's guidance is to
                    # raise the budget for that case, which is a config decision,
                    # not something a retry here could decide.
                    if exc.finish_reason == "length" or session.thinking_retries:
                        raise
                    session.thinking_retries += 1
                    books.note_strategy_nudge()
                    self.reporter.report_degraded(
                        "model_step",
                        "the model thought and stopped without answering",
                        "asked it once to finish the answer",
                    )
                    session.add_message(
                        user_message(
                            "你刚才只输出了思考内容，没有给出最终答案。"
                            "请直接给出最终答案。",
                            now_utc(),
                            synthetic=True,
                        )
                    )
                    self.closer.checkpoint(session, index)
                    continue
                except EmptyStepError:
                    # H (13 §2.7): one silent retry before the turn fails -- the
                    # same request, no injected message (dsh llm-retry parity,
                    # conservative bound of 1).  The second empty step keeps the
                    # failure semantics exactly as before.
                    if books.empty_retries >= 1:
                        raise
                    books.note_empty_retry()
                    self.reporter.report_degraded(
                        "model_step",
                        "the model returned an empty step (no text, tools, or reasoning)",
                        "retried the same request once",
                    )
                    continue
                except EngineError as exc:
                    if (
                        exc.kind == _CONTEXT_OVERFLOW
                        and overflow_compacts < _MAX_OVERFLOW_COMPACTS
                    ):
                        overflow_compacts += 1
                        report = await self.compactor.compact_now(
                            session,
                            timeout=self._remaining(frame.deadline) or _REACTIVE_COMPACT_SECONDS,
                            reason="overflow",
                        )
                        if not report["removed"]:
                            # Nothing shrank: the history is already as small as this
                            # pipeline can make it, so the rejection is the truth and
                            # another attempt would earn the same answer.
                            raise
                        self.reporter.report_degraded(
                            "model_step",
                            "provider rejected the request as too long",
                            "compacted the history and retried the step",
                        )
                        # Re-run the iteration so the retry goes through the normal
                        # path: a fresh assembly (new tail blocks, new estimates) over
                        # the history that was just rewritten.  Inlining the retry
                        # here would mean assembling a request by hand.
                        continue
                    if (
                        exc.kind != _CONTEXT_OVERFLOW
                        and books.stream_deltas > 0
                        and books.stream_continued == 0
                    ):
                        # 13 §2.5 (B): text was already on screen, so a full retry
                        # would duplicate it and a failure would drop the half the
                        # operator saw.  Ask for the continuation instead -- once.
                        books.note_stream_continue()
                        session.add_message(
                            user_message(_STREAM_CONTINUE, now_utc(), synthetic=True)
                        )
                        self.closer.checkpoint(session, index)
                        continue
                    raise

                if not step.tool_calls:
                    # R0-1: a turn with contracts does not end on the model's
                    # word — the proof commands run first, and the policy sees
                    # only their verdicts (its purity is what keeps the stop
                    # decision table-testable).
                    await self._verify_acceptance(session, books)
                    snapshot = books.snapshot(
                        iteration=index,
                        tokens_spent=self.reporter.tokens_spent(),
                        open_items=task.open_items(),
                        final_text=step.text,
                        acceptance=acceptance_states(session.acceptance),
                        continuations=session.continuations,
                    )
                    snapshot = replace(
                        snapshot,
                        mode=session.mode,
                        readonly=session.readonly,
                        intervention_ledger=self._settled_interventions(
                            session, task, snapshot
                        ),
                    )
                    decision = self.policy.decide_after_step(snapshot)
                    self._note_intervention(session, decision, snapshot)
                    if decision.action is TurnAction.FINISH:
                        outcome = TurnOutcome(
                            status=TurnStatus.COMPLETED,
                            text=step.text,
                            iterations=books.total_iterations,
                            reason=decision.reason,
                            # stage 4 r2: the plan guard's terminal line (A4:
                            # the decision carries it, so every way a turn can
                            # end shows the same note).
                            note=decision.note,
                        )
                        break
                    if decision.intervention == "acceptance":
                        books.note_acceptance_nudge()
                    elif decision.reason == "plan unsubmitted":
                        # stage 4 r2: the guard's one remedy, counted so the
                        # snapshot stops offering it next time.
                        books.note_plan_nudge()
                    else:
                        books.note_nudge()
                    session.add_message(
                        user_message(decision.inject or "", now_utc(), synthetic=True)
                    )
                    self.closer.checkpoint(session, index)
                    continue

                outcomes = await self._run_tools(
                    step,
                    session=session,
                    task=task,
                    books=books,
                    conclusion=conclusion,
                    history=assembled.history,
                    instructions=assembled.instructions,
                    deadline=frame.deadline,
                    index=index,
                    turn=frame.turn_number,
                )
                books.note_outcomes(outcomes)
                # I2: check constraint violations after tool execution
                router = getattr(self.assembler, '_router', None)
                if router is not None:
                    used_tools = [
                        item.call.name for item in outcomes
                        if item.result.success
                    ]
                    active_constraints = getattr(
                        session, "constraint_tracking", {}
                    )
                    if active_constraints:
                        router.check_constraint_violations(
                            list(active_constraints.keys()),
                            used_tools,
                            session,
                        )
                if any(item.halted for item in outcomes):
                    outcome = stopped_outcome(step.text, books.total_iterations)
                    break
                # N1: the research exit closes the turn.  A conclusion is a
                # terminal answer: nothing was planned, nothing is armed,
                # nothing runs, and no review card was ever shown.  The turn
                # ends at the batch, exactly like the dismissal below.
                #
                # It comes first because the successful exit owns the turn's
                # note: when the model gave its conclusion *and* the human
                # dismissed the review in the same batch, the conclusion is
                # what the user is waiting for (the dismissal is their own
                # action, and nothing was planned or executed either way).
                if any(
                    item.call.name == "finish_research" and item.result.success
                    for item in outcomes
                ):
                    outcome = TurnOutcome(
                        status=TurnStatus.COMPLETED,
                        text=step.text,
                        iterations=books.total_iterations,
                        reason="research finished",
                        note=PLAN_RESEARCH_NOTE,
                    )
                    break
                # N1: the plan exit closes the turn too.  An approval is the
                # exit's success (占位 = 成功): the human said yes, the plan is
                # armed, and the runner is about to execute it -- so the
                # planning round has nothing left to say.
                #
                # This branch was missing, and the model kept exploring after
                # the review card came back approved: the planning budget ran
                # out, the turn ended *badly*, and a badly ended turn is the
                # one thing that makes the close take the approval back (W2) --
                # the operator had approved a plan and the system silently
                # undid it (measured: scripts/plan_lifecycle_live_check.py,
                # the [swap] scenario).
                #
                # It reads the turn's conclusion, not the batch's results: the
                # holder is the one thing that says "this turn has an exit"
                # (present_plan sets it on approval, present_plan_tool.py:324),
                # and the two exits are mutually exclusive by construction --
                # so a research conclusion and an approval cannot ride the
                # same batch, and the order of the two branches is free.
                #
                # It comes after the halt above, because a batch the operator
                # stopped did not end cleanly, and the withdrawal at the close
                # is the right answer for that (W2).
                if conclusion.exit == "present_plan":
                    outcome = TurnOutcome(
                        status=TurnStatus.COMPLETED,
                        text=step.text,
                        iterations=books.total_iterations,
                        reason="plan approved",
                    )
                    break
                # R08 (dsh): a dismissed review (esc / empty on the card) ends
                # the turn right here -- the user said "not now", not "not
                # this".  No nudge, no arm, no auto-run; the note tells the
                # operator nothing is pending.  Reads this batch's outcomes
                # directly: the state is local, nothing needs remembering.
                if any(
                    item.call.name == "present_plan"
                    and isinstance(item.result.data, dict)
                    and item.result.data.get("dismissed") is True
                    for item in outcomes
                ):
                    # B (audit fix): the note below claims nothing ran, so
                    # nothing may stay armed -- this is the only branch that can
                    # meet a dismissal while a plan is armed (the tool refuses a
                    # second submission otherwise).  Only the plan profile is
                    # the review's to disarm; a goal task has its own lifecycle.
                    if (
                        getattr(task, "armed", False)
                        and getattr(task, "armed_profile", "") == "plan"
                    ):
                        task.disarm()
                    outcome = TurnOutcome(
                        status=TurnStatus.COMPLETED,
                        text=step.text,
                        iterations=books.total_iterations,
                        reason="plan dismissed",
                        note=PLAN_DISMISSED_NOTE,
                    )
                    break
        except asyncio.CancelledError:
            # Record what happened, persist the partial transcript, then let the
            # cancellation propagate — swallowing it is an asyncio anti-pattern.
            self._close_turn(
                session,
                user_input=user_input,
                task=task,
                frame=frame,
                books=books,
                outcome=aborted_outcome(books.last_text, books.total_iterations),
            )
            raise
        except (DeadlineExpired, EngineError, EmptyStepError, ThinkingOnlyStepError) as exc:
            outcome = failed_outcome(
                exc, last_text=books.last_text, iterations=books.total_iterations
            )

        return await self._finish_turn(
            session,
            user_input=user_input,
            task=task,
            frame=frame,
            books=books,
            outcome=outcome,
            closing=closing,
        )

    async def _begin_turn(
        self,
        session: SessionData,
        user_input: str,
        observer: LoopObserver | None,
        images: tuple[ImageRef, ...] = (),
        *,
        append_user: bool = True,
        synthetic: bool = False,
    ) -> TurnFrame:
        """Everything that must happen before the first iteration.

        Binding the UI, counting the turn, clearing per-turn policy state and
        asking ``turn.start`` hooks — none of it is a decision, so it does not
        belong in the loop body (docs/r7-analysis.md 3.1).
        """
        self._bind_observer(observer)
        self._bind_trace(session)
        # M3: point ToolObserver to current session's observations list
        self._tool_observer._observations = session.tool_observations
        # M4: point SkillTool load callback to current session's stats
        # F1: also track loaded skill for outcome recording
        from ai_coding.tools.skill_tool import SkillTool
        skill_tool = self.tools.get("skill")
        router = getattr(self.assembler, '_router', None)
        if isinstance(skill_tool, SkillTool) and router:
            def _on_skill_loaded(
                name: str,
                _router: Any = router,
                _session: SessionData = session,
            ) -> None:
                _router.record_load(name, _session.turn_count, _session.skill_stats)
                _session.loaded_skill = name
            skill_tool.set_on_loaded(_on_skill_loaded)
        # Q1 (08): resume reuses the persisted history -- appending the prompt
        # again would duplicate the user's line and start a new turn over it.
        if append_user:
            session.add_message(user_message(user_input, now_utc(), images, synthetic=synthetic))
        # T5: remember this request's instruction so compaction can keep it in
        # the history (hotel audit root cause R5: the instruction at index 80 fell
        # out of the L1 tail window and the model re-anchored on old content).
        # A resumed turn appends nothing, so the anchor is the last thing the
        # human actually typed -- not messages[-1] (a tool result), and not a
        # synthetic harness line in the model's own voice.
        anchor: ChatMessage | None = None
        if append_user:
            self._current_instruction = session.messages[-1]
        else:
            anchor = next(
                (
                    message
                    for message in reversed(session.messages)
                    if message.role == "user" and not message.synthetic
                ),
                None,
            )
            self._current_instruction = anchor
        # R6a: the session-level turn counter.  It is what `source_turn` and the
        # <working-memory> header mean by "turn N" (docs/r6-analysis.md 2.4).
        # A resumed turn is the *same* turn continued (08/Q2), so the counter
        # -- and with it open_turn below -- stays put.
        if append_user:
            session.turn_count = int(getattr(session, "turn_count", 0) or 0) + 1
        self.closer.begin_turn()
        if session.task is None:
            if append_user:
                session.task = TaskState(goal=user_input)
            else:
                # A bare-goal task never reaches the file -- to_dict drops it
                # as noise (domain/session.py) -- so a session that crashed
                # before any todo item loads with task None.  Rebuild the goal
                # from the anchor above: it is the object the dead turn had.
                session.task = TaskState(
                    goal=anchor.content if anchor and anchor.content else ""
                )
        elif (
            append_user
            and not session.task.items
            and session.mode != "plan"
            # D-4: an armed task owns its goal field -- a goal round's prompt
            # is an instruction *about* the goal, and letting it overwrite the
            # objective would erase what every later round is measured against.
            and not getattr(session.task, "armed", False)
        ):
            # The bare goal tracks the turn's own instruction.  Measured
            # (session d68080eb): the opening "你好" froze here and rode every
            # later request's <task-state> tail block; after a long tool chain
            # the model anchored on that tail and answered the greeting again.
            # A list with items keeps its goal -- it names what the items are for.
            # 18 §10.5: nor does it change in plan mode — the goal topic belongs
            # to the planning axis and a new turn must not steal it.
            session.task.goal = user_input
        task = session.task
        # C3: the continuation allowance is per user turn — a new message
        # starts with a fresh budget, not the previous turn's leftovers.
        session.continuations = 0
        # R4b: guard state (duplicate calls, …) is per turn, never per process.
        self.dispatcher.guards.reset()
        self.reporter.begin_turn(self.observer)
        self._warn_tools_chars_once()
        # P2 (补充式方案 06) mark this turn open; 11-spec §7.1 makes it
        # durable immediately.  The old design rode the *next* save ("a crash
        # before that first save has no output to lose") -- true for outputs,
        # false for the window we care about now: it contains the USER's
        # message, and an armed task must see both the input and the open
        # marker after a crash.  One save, only for real user turns; resume's
        # own save covers its path; ephemeral sessions are skipped by the
        # closer itself.  ``_close_turn`` clears the marker on every exit.
        session.open_turn = session.turn_count
        if append_user:
            self.closer.save(session)
        # F (13 §2.4): one subprocess per turn; never raises (see infra.vcs).
        session.git_branch = git_branch(self.workspace)
        hook_context = await self.turn_hooks.context(session)
        turn_timeout = float(getattr(self.settings, "turn_timeout_seconds", 0.0) or 0.0)
        return TurnFrame(
            turn_number=session.turn_count,
            task=task,
            hook_context=hook_context,
            deadline=Deadline.after(turn_timeout) if turn_timeout > 0 else None,
        )

    def _take_notices(self, session: SessionData, index: int) -> None:
        """Absorb what the harness learned about this run, at the same boundary.

        The sibling of ``_take_steering`` with three deliberate differences: the
        marker is ``[notice]`` (the replay renders a ``[steering]`` line as the
        operator's own words, and a settled background task is not), the stall
        ladder is left alone (a finished task is not user intervention), and no
        event is emitted (the operator already has ``BackgroundTaskDone``, and a
        second notice would say the same thing twice).

        The lines are persisted like every other harness message, so a notice
        that lands while no turn is running waits for the next turn rather than
        being lost.
        """
        if self.notices is None:
            return
        lines = self.notices.drain()
        if not lines:
            return
        for line in lines:
            session.add_message(user_message(f"[notice] {line}", now_utc(), synthetic=True))
        self.closer.checkpoint(session, index)

    def _take_steering(self, session: SessionData, books: TurnBooks, index: int) -> None:
        """Absorb whatever the user typed, right before this request is assembled.

        Delivery point: the iteration boundary, after the previous batch finished -
        which is what Claude Code documents ("as soon as those tool calls finish,
        within the same turn", docs/r7e-analysis.md 2.1b).  The lines are persisted
        (Q4), and they consume neither an iteration nor a nudge.

        Reads the session's inbox table, next-step rows only (D-2 Phase 2): the
        old in-memory FIFO and this table agreed on nothing, and only one of
        them survived a restart.  Next-turn rows stay parked -- a steer-scoped
        drain never promotes them (oc's execution.ts draws the same line).
        """
        rows = [item for item in session.inbox if item.target == "next-step"]
        if not rows:
            return
        session.inbox = [item for item in session.inbox if item.target != "next-step"]
        lines = [row.text for row in rows]
        for line in lines:
            session.add_message(user_message(f"[steering] {line}", now_utc(), synthetic=True))
        books.note_steering(len(lines))
        self.closer.checkpoint(session, index)
        self._emit(
            InputSteered(
                text_chars=sum(len(line) for line in lines),
                iteration=index,
                total=books.steering_messages,
            )
        )

    def _warn_tools_chars_once(self) -> None:
        """Say once, per loop, that the MCP tool surface is over its ceiling.

        r22: judged on the MCP part, not the whole surface.  The built-in tools
        are a fixed cost the user cannot change, and they had grown past the old
        whole-surface ceiling, so the warning fired for every session and told
        nobody anything they could act on.  ``origin`` is ``"local"`` for a
        built-in tool and an MCP server's name otherwise (``tools/base.py``), so
        everything that is not local is what the user configured.
        """
        if self._tools_warned:
            return
        threshold = int(getattr(self.settings, "mcp_chars_warn", 0) or 0)
        mcp_chars = sum(
            chars for origin, chars in self._tools_by_origin.items() if origin != "local"
        )
        if threshold <= 0 or mcp_chars <= threshold:
            return
        self._tools_warned = True
        biggest = next(
            (origin for origin, _ in self._tools_by_origin.items() if origin != "local"), ""
        )
        self._emit(
            ToolSurfaceWarning(
                tools_chars=self._tools_chars,
                mcp_chars=mcp_chars,
                threshold=threshold,
                origins=tuple(self._tools_by_origin.items()),
                hint=(
                    "MCP 工具 schema 每一步都会重发；"
                    f"最贵的是 '{biggest}'。用 mcp.servers[].tools 只注册需要的工具，"
                    "或关掉用不到的 server。"
                ),
            )
        )

    async def _finish_turn(
        self,
        session: SessionData,
        *,
        user_input: str,
        task: TaskState,
        frame: TurnFrame,
        books: TurnBooks,
        outcome: TurnOutcome,
        closing: str = "",
    ) -> str:
        """Close the books: persist, harvest, fire ``turn.end``, hand back the text."""
        # F1: record skill outcome for dynamic pricing
        # F3: record structural signature on skill success
        router = getattr(self.assembler, '_router', None)
        if router and session.loaded_skill:
            stats = getattr(session, 'skill_stats', {})
            success = not books.failures
            router.record_outcome(session.loaded_skill, success, stats)
            if success and task:
                from ai_coding.core.sop_router import StructuralSignature
                error_keywords = frozenset(
                    word.lower()
                    for obs in getattr(session, "tool_observations", [])
                    if obs.error
                    for word in obs.error.lower().split()
                    if len(word) > 2
                )
                task_keywords = frozenset(
                    word for word in task.goal.lower().split() if len(word) > 2
                ) if task.goal else frozenset()
                signature = StructuralSignature(
                    tools=frozenset(books.tool_success),
                    error_keywords=error_keywords,
                    task_keywords=task_keywords,
                    skill_name=session.loaded_skill,
                    timestamp=now_utc(),
                )
                router.record_signature(signature)
        outcome = self._close_turn(
            session,
            user_input=user_input,
            task=task,
            frame=frame,
            books=books,
            outcome=outcome,
            closing=closing,
        )
        # 2026-10-17: the harvest runs after the response now, not inside the
        # turn's future.  Scheduled here -- right where it used to be awaited --
        # so the delta's provenance (turn number, evidence flag, summaries)
        # still comes from this turn's books.
        self._schedule_harvest(session, frame=frame, books=books, outcome=outcome)
        await self.turn_hooks.fire(HookPoint.TURN_END, session)
        # M1: schedule a background skill review if the interval has been reached.
        self._maybe_schedule_skill_review(session)
        # R6a: a turn that ran out of budget used to hand the user an empty string.
        return outcome.text or outcome.handoff

    # ---- skill review (M1) --------------------------------------------------

    def _schedule_harvest(
        self,
        session: SessionData,
        *,
        frame: TurnFrame,
        books: TurnBooks,
        outcome: TurnOutcome,
    ) -> None:
        """Run this turn's memory harvest after the response (2026-10-17).

        The harvest used to sit in ``_finish_turn`` and hold the turn's future
        open for as long as the extractor took -- measured live at 0-22s, and
        that whole window was one LLM call.  Every waiter downstream (the wake
        latch's runway, promote's settle-wait) was paying it.  The fix follows
        the skill review's existing pattern: schedule, don't await -- "the
        review runs after the user has received its response" is the same
        sentence here.

        Chained on purpose: the harvest captures its delta lazily (everything
        since the cursor), so two overlapping harvests would show the same
        messages to the extractor twice.  The chain also coalesces deltas when
        turns outrun extraction -- by design: the cursor only advances over
        what the extractor was shown, so nothing is ever skipped, and a retry
        after a failure naturally includes everything since the last success.
        """
        if not self.turn_memory.enabled:
            return
        previous = self._harvest_task

        async def run_harvest() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:  # noqa: BLE001 — a chain link must not break the next
                    pass
            await self.turn_memory.harvest(
                session,
                deadline=frame.deadline,
                summaries=books.summaries,
                outcome=outcome,
                # R6d: the turn's provenance for whatever it wrote — a fact the loop
                # already has, so memory can record where a note came from without
                # asking the model (and without filtering on it: D-R6-10).
                turn=frame.turn_number,
                # R8a D-14: what counts is evidence about the world, and a checkpoint
                # write is the turn talking about itself.
                evidence="tool" if books.evidence_calls else "model",
            )

        self._harvest_task = asyncio.create_task(
            run_harvest(), name=f"memory-harvest:{session.session_id}"
        )

    async def flush_harvest(self) -> None:
        """Await the pending harvest chain (tests; ``aclose`` bounds it).

        Never raises: the harvest is best-effort by contract, and a waiter
        must not resurrect a failure the harvest already swallowed.
        """
        while True:
            task = self._harvest_task
            if task is None or task.done():
                return
            try:
                await task
            except Exception:  # noqa: BLE001 — see above
                return

    def _maybe_schedule_skill_review(self, session: SessionData) -> None:
        """Schedule a background skill review when the interval is reached.

        Best-effort: the review runs after the user has received its response,
        so a slow or failed review never blocks the conversation.
        """
        interval = getattr(self.settings, "skill_review_interval", 0) or 0
        if interval <= 0:
            return
        if self.skills is None:
            return
        counter = int(getattr(session, "skill_review_counter", 0) or 0) + 1
        session.skill_review_counter = counter
        if counter < interval:
            return
        session.skill_review_counter = 0
        # Fire-and-forget: review runs after the user has received their response.
        asyncio.create_task(
            self._run_skill_review(session),
            name=f"skill-review:{session.session_id}",
        )

    async def _run_skill_review(self, session: SessionData) -> None:
        """Run the skill review in the background.  Best-effort, never raises."""
        from ai_coding.core.skill_review import run_skill_review
        cursor = int(getattr(session, "skill_review_cursor", 0) or 0)
        delta = session.messages[cursor:]
        if not delta:
            return
        try:
            model_config = getattr(self.engine, "model_config", None)
            if model_config is None:
                return
            result = await run_skill_review(
                messages=delta,
                existing_skills=self.skills.names() if self.skills else [],
                model_config=model_config,
                skill_dir=self.skills.root if self.skills else Path("."),
            )
            if result.created:
                session.skill_review_cursor = len(session.messages)
                _log.info(
                    "skill review created: %s at %s",
                    result.skill_name, result.skill_path,
                )
        except Exception:  # noqa: BLE001 — best-effort, never affect the user
            _log.debug("skill review failed", exc_info=True)

    def _close_turn(
        self,
        session: SessionData,
        *,
        user_input: str,
        task: TaskState,
        frame: TurnFrame,
        books: TurnBooks,
        outcome: TurnOutcome,
        closing: str = "",
    ) -> TurnOutcome:
        """Record what this turn leaves behind, then persist it (R6a)."""
        # W1: every exit funnels through here (normal, failed, and the
        # cancelled path above), so the length-cutoff flag lands uniformly —
        # including on turns the budget or the clock stopped.
        outcome = replace(
            outcome, truncated_by_length=(books.last_finish_reason == "length")
        )
        # P2 (补充式方案 06): every exit funnels through here, so this is where
        # the open marker is cleared -- the close below persists both the
        # outcome and the cleared marker in one write.
        session.open_turn = None
        # 13 §2.1 (D5): the turn boundary settles what it cut short — a pending
        # window becomes unfinished, so the next turn retries the same level.
        self._settled_interventions(
            session,
            task,
            books.snapshot(
                iteration=books.iterations,
                tokens_spent=0,
                open_items=task.open_items(),
            ),
            turn_end=True,
        )
        # 18 §10.2: a flip queued while this turn ran lands here, so the close's
        # own save carries it to disk (zero new save points).
        self._apply_pending_posture(session)
        # M2 T3: a reminder queued while this turn ran lands here too, so the
        # close's own save carries it (zero new save points).
        self._apply_pending_reminders(session)
        # W2: an approval belongs to the turn that carried it.
        #
        # The plan was written against the state this turn ended in.  A turn
        # that did not end cleanly (error / timeout / budget / max_iterations
        # / stopped) left the session somewhere else, and the front end
        # already refuses to start a run from it -- so the backend must not
        # leave the plan armed on disk either, or the boot resume would run
        # exactly what the front end declined to run.  The two answers have
        # to agree.
        #
        # A turn that ended cleanly keeps the approval: that is what the
        # approval was for, and the run it authorises still has to happen.
        #
        # A crash is a different thing: the close never runs, so the
        # approval stays armed and the boot resume continues the work the
        # human agreed to (D-02's crash point ①).  Only the plan profile is
        # touched -- a goal's arm belongs to the goal runner, which keeps
        # running across turns by design.
        #
        # Before the close, so the close's own save carries the withdrawal
        # (zero new save points).
        if (
            books.plan_approved
            and outcome.status is not TurnStatus.COMPLETED
            and getattr(task, "armed", False)
            and getattr(task, "armed_profile", "") == "plan"
        ):
            task.disarm()
            # D2: the withdrawal has to leave a trace.  The human approved
            # something and the system took it back; without a line about
            # it the screen shows nothing at all, and the plan simply never
            # runs -- the one thing the operator could not see is the one
            # thing that was taken away.
            #
            # Mutually exclusive with the guard's note (the guard does not
            # fire for a turn that submitted), so the two never need
            # patching together.
            outcome = replace(outcome, note=PLAN_APPROVAL_WITHDRAWN_NOTE)
        return self.closer.close(
            session,
            user_input=user_input,
            task=task,
            turn_number=frame.turn_number,
            outcome=outcome,
            failures=books.failures,
            trace_path=self._trace_path(session),
            observer=self.observer,
            # R8a: the line the model wrote for its future self, if it wrote one.
            model_note=books.checkpoint or "",
            # G8-2: the wrap-up the model wrote when asked to close the turn.
            closing=closing,
        )

    def _bind_observer(self, observer: LoopObserver | None) -> None:
        """Honour a per-call observer (the REPL swaps the UI between turns).

        The trace recorder rides along in the same braid.  It used to be the
        caller's job to compose the two (``cli._compose_observer``, which exists
        and has no callers) — but a per-call observer *replaces*
        ``self.observer``, so anything composed at construction time was thrown
        away on the first turn.  Measured consequence in the Textual app: the
        recorder saw MCP events only, so no iteration, no tool call and no
        ``TurnFinished`` reached ``traces/`` — and with no ``TurnFinished`` there
        is no ``summary.json`` and no ``capture: full`` snapshot either.

        Composing here rather than at the call site keeps the swap semantics
        intact and makes every front-end (TUI, REPL, a future CLI) get it for
        free.  ``Braid`` isolates each sink, so a failing UI cannot silence the
        recorder, and both are wrapped because either may misbehave.

        A caller that passes *no* observer still gets the trace, off the
        construction-time base: the REPL does exactly that, and the first
        end-to-end run of this change produced an empty trace directory for
        precisely this reason.  With no trace *and* no per-call observer the
        current observer is left untouched, which is what callers assigning
        ``loop.observer`` directly rely on.
        """
        if observer is None and self.trace is None:
            return          # nothing to swap: leave the current observer alone
        base = observer if observer is not None else self._base_observer
        # M3: ToolObserver auto-records tool call outcomes (always included)
        tool_obs = SafeObserver(self._tool_observer)
        if self.trace is not None:
            self.observer = Braid(SafeObserver(base), SafeObserver(self.trace), tool_obs)
        else:
            self.observer = Braid(SafeObserver(base), tool_obs)
        self.dispatcher.observer = self.observer

    def _bind_trace(self, session: SessionData) -> None:
        """Point the trace at the current session (full capture reads it).

        An ephemeral session (a plan step) is deliberately not a session as
        far as the trace is concerned: its transcript is discarded on
        purpose, so letting it take the session id scattered ``plan-*``
        directories, split the run's token spend into a summary the parent
        never shows, and left the recorder pointing at a dead id after the
        run.  The id therefore stays on the parent; the step's messages are
        still attached, so ``capture: full`` snapshots what actually ran.
        """
        if self.trace is None:
            return
        if not session.ephemeral:
            self.trace.session_id = session.session_id
        self.trace.attach_session(session, start=len(session.messages))

    def _trace_path(self, session: SessionData) -> str:
        """Where this turn's trace lives, for the handoff (best effort)."""
        path = getattr(self.trace, "current_path", None)
        return str(path) if path else ""

    def _note_approval(
        self, session: SessionData, pending: dict[str, Any] | None
    ) -> None:
        """P3 (补充式方案 06): mirror a pending approval into its own marker.

        Deliberately not a session save: the wait sits inside the tool batch,
        ahead of any checkpoint, and ``TurnCloser.save`` normalizes tool pairing
        -- which drops an assistant carrier whose result has not been written
        back yet, destroying the pending call (verified).  A marker file keeps
        the session untouched, so nothing about the saved history changes.
        """
        session.pending_approval = pending
        self.sessions.mark_pending_approval(session.session_id, pending)

    def _effective_session_rules(self, session: SessionData) -> tuple[Rule, ...]:
        """This session's layer plus what a parent handed this loop.

        The child's own session starts empty, so the second half is how a
        parent's rules reach delegated work (M1a ④).  One method because the
        dispatch context, the catalogue, the prompt and acceptance must all
        read the same effective set.
        """
        return (*self._session_rules(session), *self._session_permissions)

    def _agent_rules(self, session: SessionData) -> tuple[Rule, ...]:
        """The current agent's rule layer, parsed once per agent id.

        The catalog stores the **rows** (``domain`` may not import
        ``security``); the reader is the same one the session layer uses, so
        an agent file and a session file are read by one definition of what a
        rule is.  A loop with no agent table has no agent layer — which is
        what keeps every existing construction site byte-for-byte compatible.
        """
        if self.agents is None:
            return ()
        agent_id = str(getattr(session, "agent", "") or "")
        cached = self._agent_rule_cache.get(agent_id)
        if cached is not None:
            return cached
        spec = self.agents.get(agent_id)
        if spec is None:
            # An id nobody knows: no agent layer.  ``resolve`` deliberately
            # falls back to build's *rules* (empty), and inventing a layer for
            # a typo would change behaviour for a session that never opted in.
            rules: tuple[Rule, ...] = ()
            errors: tuple[str, ...] = ()
            if agent_id:
                errors = (f"unknown agent {agent_id!r}; no agent rules applied",)
            self._agent_rule_cache[agent_id] = rules
            self._agent_rule_errors[agent_id] = errors
            return rules
        else:
            # The catalog owns the parse (M2 T6): one reader, one place.
            rules = self.agents.rules(agent_id)
            errors = ()
        if errors:
            _log.warning("agent permission rules: %s", "; ".join(errors))
        self._agent_rule_cache[agent_id] = rules
        self._agent_rule_errors[agent_id] = errors
        return rules

    def _agent_body(self, session: SessionData) -> str:
        """The agent's own instructions, or ``""``."""
        if self.agents is None:
            return ""
        spec = self.agents.get(str(getattr(session, "agent", "") or ""))
        return spec.body if spec is not None else ""

    @staticmethod
    def _session_rules(session: SessionData) -> tuple[Rule, ...]:
        """This session's stored layer, with broken rows reported once.

        The raw rows stay on the session; skipping a bad one is the only
        safe reading, and ``/permissions`` shows the reasons.  Logged only
        when the set of reasons changes, so a bad row does not write a
        warning on every batch of every turn.
        """
        rules, errors = rules_from_dicts(getattr(session, "permissions", ()) or ())
        if errors and errors != tuple(getattr(session, "permission_errors", ()) or ()):
            _log.warning("session permission rules: %s", "; ".join(errors))
        session.permission_errors = errors
        return rules

    async def _run_tools(
        self,
        step: StepResult,
        *,
        session: SessionData,
        task: TaskState,
        books: TurnBooks,
        history: list[ChatMessage],
        instructions: str | None,
        deadline: Deadline | None,
        index: int,
        turn: int = 0,
        conclusion: TurnConclusion | None = None,
    ) -> list[DispatchOutcome]:
        """Run this step's tool calls and hand back what they did.

        R6b needs to see the outcomes (was anything attempted, did anything work,
        what failed last), so the caller — not this method — keeps the counters.
        Dispatch, write-back, the todo event and the mid-turn checkpoint all happen
        here, so the loop body reads as the ReAct cycle it is.
        """
        limit = int(getattr(self.settings, "working_memory_note_chars", 120) or 120)
        if self.cancel is not None:
            # R8c: one Escape drops the batch that is starting, not the whole turn.
            self.cancel.clear()
        session_rules = self._effective_session_rules(session)
        agent_rules = self._agent_rules(session)
        outcomes = await self.dispatcher.dispatch_batch(
            step.tool_calls,
            tool_context(
                task=task,
                workspace=self.workspace,
                session=session,
                history=history,
                instructions=instructions,
                emit=self._emit,
                turn=turn,
                # M1a: the effective layer (own + inherited).  Parsed per
                # batch, so a rule added between turns is in force on the
                # next request.
                permissions=session_rules,
                agent_rules=agent_rules,
                jobs=self.jobs,
                # W2/R02: the operator's declared roots ride along, so a tool
                # that judges reachability (present_plan's gate) answers with
                # the same roots the workspace gate uses.
                extra_workspace_roots=self.extra_workspace_roots,
                # R8a: the only channel a tool has to leave something in the
                # turn's book (checkpoint_write uses it).
                note_checkpoint=lambda text: books.note_checkpoint(text, limit=limit),
                # P3 (补充式方案 06): the dispatcher announces a pending approval
                # here and clears it when answered; the loop mirrors it into a
                # marker beside the session file, never into the session itself.
                note_approval=lambda pending: self._note_approval(session, pending),
                # R10: approval is a new plan episode -- the session's stale
                # ladder windows must not ride into it (begin_plan's sibling).
                # W2: and the books remember that this turn approved a plan,
                # so the turn's close can withdraw the approval if the turn
                # did not end cleanly (the front end already refuses to start
                # a run from such a turn; the backend must not leave the plan
                # armed on disk for the boot resume to pick it up).
                note_plan_approved=lambda: _on_plan_approved(session, books),
                # N1: the turn's conclusion holder -- the two exits of a
                # plan-mode turn read it to stay mutually exclusive.
                conclusion=conclusion,
            ),
            deadline=deadline,
            iteration=index,
            view=TurnView(
                iteration=index,
                tokens_spent=self.reporter.tokens_spent(),
                task_open=len(task.open_items()),
                # 13 §2.2: the L2 window is the guard's arming — the ledger is
                # the single source of "is the ladder's refusal phase open".
                strict_repeats=interventions.armed(session.intervention_state),
            ),
        )
        self.closer.write_back(session, outcomes)
        if any(
            # ``present_plan`` joined the list with P1: approval is what writes
            # the submitted items into the task, and the plan card is driven by
            # this event -- a write the frontend never hears about is a plan it
            # never shows.  (During planning, todo_write is prompt-banned, so
            # nothing else would emit it either.)
            item.call.name in ("todo_write", "start_goal", "update_goal", "present_plan")
            for item in outcomes
        ):
            self._emit(TaskUpdated(task=task))
        self.closer.checkpoint(session, index)
        return outcomes

    def _refresh_ledger(self, books: TurnBooks) -> None:
        """Derive the open unknowns and emit when the set moves (R0-2).

        Emission is edge-triggered on the signature: a question appears once
        in the transcript, not as a per-round heartbeat.
        """
        items = books.ledger_items(
            stall_threshold=self.policy.config.stall_iterations
        )
        if books.note_ledger(ledger_signature(items)) and items:
            self._emit(LedgerUpdated(items=tuple(items)))

    def _settled_interventions(
        self,
        session: SessionData,
        task: TaskState,
        snapshot: TurnSnapshot,
        *,
        turn_end: bool = False,
    ) -> dict[str, Any]:
        """Advance ladder windows and put the ledger back on the session (13 §2.1).

        Two reset boundaries live here because they need the task object: a
        finished task (items exist and are all terminal) starts the next task
        from a clean ledger; everything else rides the existing save rhythm —
        this method never saves on its own.
        """
        if task.items and task.is_complete():
            session.intervention_state = None
            return {}
        ledger = interventions.settle(
            session.intervention_state or {}, snapshot, turn_end=turn_end
        )
        session.intervention_state = ledger or None
        return ledger

    def _note_intervention(
        self, session: SessionData, decision: TurnDecision, snapshot: TurnSnapshot
    ) -> None:
        """Persist one fired decision (搭 checkpoint/close 的车落盘)."""
        if not decision.intervention:
            return
        # B5: only the search ladder emits.  The other four gates already leave
        # their evidence in the counters, and emitting for all five would change
        # every existing trace.  Only the *nudge* path reaches here: a ladder that
        # chose FINISH goes through ``_finish_at``, which drops the intervention
        # metadata -- a known defect (docs/plan-m-b.md P1-1), out of this scope.
        if decision.intervention == interventions.SEARCH:
            self._emit(
                SearchStalled(
                    iteration=snapshot.iteration,
                    level=decision.level,
                    calls=snapshot.search_calls,
                    streak=snapshot.search_streak,
                )
            )
        session.intervention_state = interventions.record(
            session.intervention_state or {}, decision, snapshot
        )

    async def _verify_acceptance(self, session: SessionData, books: TurnBooks) -> None:
        """Run the proof commands for unchecked acceptance items (R0-1).

        Only items that never passed are checked, so a green suite is not
        re-run every iteration.  Verdicts land on the session items (persisted
        by the normal save) and in the books (for the policy snapshot and the
        uncertainty ledger) — the policy itself never touches a shell.
        """
        # 18 §10.5: a read-only session promised not to run anything — the
        # proof commands are shell and bypass the dispatch gate, so they stop
        # here.  Items stay pending (fail-closed downstream) until a writable
        # turn verifies them.
        if session.readonly:
            return
        pending = pending_items(session.acceptance)
        if not pending:
            return
        each = float(getattr(self.settings, "acceptance_verify_each_seconds", 120.0) or 0.0)
        total = float(getattr(self.settings, "acceptance_verify_total_seconds", 300.0) or 0.0)
        session_rules = self._effective_session_rules(session)
        agent_rules = self._agent_rules(session)
        posture = posture_of(
            str(getattr(session, "mode", "normal") or "normal"),
            bool(getattr(session, "readonly", False)),
        )
        checked = await verify_items(
            pending,
            self.workspace,
            timeout_each=each or 120.0,
            timeout_total=total or 300.0,
            # W2: a proof command is a shell command, and it runs outside the
            # dispatch gate -- so this is where a human gets to answer for
            # one that reaches outside the workspace.  The dispatcher's
            # callback is the same object this loop was built with.
            approval=self.dispatcher.approval,
            # M1a ③: a rule deny is not a question.  The same layer that
            # refuses a bash tool call refuses a proof command, so the shell
            # is no longer the one hole in "the rules are the boundary".
            deny_reason=lambda action, params: self.gate.deny_reason(
                action,
                params,
                session_rules=session_rules,
                agent_rules=agent_rules,
                posture=posture,
            ),
            # M3 T5: a proof naming an operator-declared root is reachable;
            # the same roots the file tools and the workspace gate use.
            extra_roots=self.extra_workspace_roots,
        )
        by_id = {item.id: item for item in checked}
        for item in session.acceptance:
            if item.id in by_id:
                item.passed = by_id[item.id].passed
                item.output = by_id[item.id].output
        # Only verdicts (pass/fail) enter the books — items skipped by the
        # total cap stay None and must not fabricate ledger entries.
        books.note_verify({
            item.id: (item.passed, item.output)
            for item in checked
            if item.passed is not None
        })

    async def _ask_for_closing(
        self,
        *,
        session: SessionData,
        task: TaskState,
        books: TurnBooks,
        frame: TurnFrame,
        previous_request: str | None,
        on_delta: DeltaSink | None,
    ) -> str:
        """One last model call asking for a summary (G8-2).

        Returns ``""`` when it is not worth asking or the answer did not come:
        the harness report is written either way, so a failure here costs
        nothing but the attempt.  Never raises -- a close-out nicety must not be
        able to turn a finished turn into a failed one.
        """
        limit = self.policy.config.max_turn_tokens
        if limit > 0 and self.reporter.tokens_spent() >= limit:
            # No room to answer: the request would be refused, and the token
            # ceiling is precisely the budget that is already gone.
            return ""
        try:
            assembled = await self.assembler.assemble(
                session=session,
                task=task,
                books=books,
                turn_number=frame.turn_number,
                hook_context=frame.hook_context,
                deadline=frame.deadline,
                index=self.policy.config.max_iterations + 1,
                previous_request=previous_request,
                pinned=(self._current_instruction,) if self._current_instruction else (),
            )
            request = [
                *assembled.request,
                user_message(_CLOSING_PROMPT, now_utc(), synthetic=True),
            ]
            step = await run_with_deadline(
                self.engine.step(
                    request,
                    # G (13 §2.7): a closing step has no next iteration to run
                    # tools in, so offering them meant silently dropping any call
                    # (probe P4).  The engine's contract: empty list = none
                    # offered (ai/engine.py), matching opencode's toolChoice:none.
                    tools=[],
                    instructions=assembled.instructions,
                    on_delta=on_delta,
                    # r17/T5 offered only when the request actually carries
                    # images: an engine (or test double) that predates the
                    # parameter stays callable for every text-only turn.
                    **(
                        {"image_loader": self._load_image}
                        if any(m.images for m in request)
                        else {}
                    ),
                ),
                timeout=self._remaining(frame.deadline),
                label="closing_step",
            )
        except Exception:  # noqa: BLE001 -- the report is still owed, and it is
            # the harness's, not the model's: a failed courtesy call must not
            # replace it with an error.
            return ""
        # The model answered, so it gets a step on the record like any other.
        self.reporter.report_step(
            index=self.policy.config.max_iterations + 1,
            step=step,
            latency_ms=0,
            model=str(getattr(getattr(self.engine, "model_config", None), "name", "") or ""),
        )
        return step.text or ""

    async def _model_step(
        self,
        *,
        session: SessionData,
        request: list[ChatMessage],
        instructions: str | None,
        deadline: Deadline | None,
        on_delta: DeltaSink | None,
        index: int,
        books: TurnBooks,
    ) -> StepResult:
        """One model call — the only wait that cannot degrade (docs/r2 §7.3).

        It is timed, reported and appended here so the loop body reads as
        "decide → ask → act" without the bookkeeping in between.
        """
        started = time.monotonic()

        def counted_delta(text: str) -> None:
            # 13 §2.5 (B): count even when there is no sink -- "did the operator
            # see part of the answer" is a fact about the stream, not the UI.
            books.note_delta()
            if on_delta is not None:
                on_delta(text)

        try:
            step = await run_with_deadline(
                self.engine.step(
                    request,
                    tools=self._surface_schemas(session),
                    instructions=instructions,
                    on_delta=counted_delta,
                    **(
                        {"image_loader": self._load_image}
                        if any(m.images for m in request)
                        else {}
                    ),
                ),
                timeout=self._remaining(deadline),
                label="model_step",
            )
        except DeadlineExpired:
            # R0-4: a step cut by the clock still happened — its streamed
            # deltas survive in the transcript, and the books count its
            # seconds instead of claiming they never elapsed (measured: a
            # 179s step vanishing).  The turn still ends exactly as before;
            # this is accounting, not a behaviour change.
            latency_ms = int((time.monotonic() - started) * 1000)
            books.note_abandoned(latency_ms)
            self.reporter.report_degraded(
                "model_step",
                "turn deadline exceeded",
                "partial deltas kept in transcript",
            )
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        books.note_step(step.text, finish_reason=step.finish_reason, latency_ms=latency_ms)
        # Who answered, not who was configured: a fallback chain may have moved
        # this step, and the spend has to land on the provider that took it
        # (G7).  Read rather than threaded through StepResult -- the chain
        # already maintains this, and a new field would touch every construction
        # site in the codebase to say something one attribute already says.
        self.reporter.report_step(
            index=index,
            step=step,
            latency_ms=latency_ms,
            model=str(getattr(getattr(self.engine, "model_config", None), "name", "") or ""),
        )
        self._emit(AssistantMessage(
            text=step.text, tool_calls=tuple(step.tool_calls),
            reasoning=step.reasoning, agent=session.agent))
        # A step that neither spoke nor acted cannot become a message, and
        # ChatMessage says so on purpose (message.py:139).  Named here so the
        # turn ends as a turn — closed, persisted, counted — instead of the
        # domain error escaping to the IPC catch-all (A6).  After the emit on
        # purpose: the thinking is still worth showing before the reason.
        if not step.text and not step.tool_calls and not step.reasoning:
            raise EmptyStepError(step.finish_reason)
        if not step.text and not step.tool_calls:
            # It thought and then stopped (M7).  The reasoning was emitted a
            # line above and is on screen, so the model *did* answer — in the
            # register this provider uses for thinking.  Calling that an engine
            # error was wrong twice over: it is not a failed call, and it told
            # the operator nothing about a model that simply ran out of room.
            # Measured against a real provider: the same model answered the
            # same question with and without MCP connected.
            #
            # It is *not* added to the session: an assistant message with
            # neither content nor tool_calls is rejected by providers on the
            # next request (message.py:139), so persisting one would poison
            # every later turn.  The reasoning went out as the event above,
            # which is what the screen shows.
            raise ThinkingOnlyStepError(step.finish_reason, len(step.reasoning or ""))
        session.add_message(assistant_message(step, now_utc(), agent=session.agent))
        return step

    async def aclose(self) -> None:
        """Release engine resources (the loop owns nothing else).

        A pending background harvest gets a bounded chance to finish first
        (2026-10-17): the extractor needs the engine that is about to close,
        and a graceful exit should not throw away the last turn's memory.
        Five seconds is the ceiling -- a slow provider must not hold up
        shutdown; past it the loss is what a crash would cost anyway.
        """
        try:
            await asyncio.wait_for(self.flush_harvest(), timeout=5.0)
        except Exception:  # noqa: BLE001 — shutdown is best-effort
            _log.debug("harvest flush on close failed or timed out", exc_info=True)
        await self.engine.aclose()

    def memory_status(self) -> dict[str, Any]:
        """Memory counts for the UI (``/memory`` / ``/status``); never raises."""
        return self.turn_memory.status()

    async def compact_session(self, session: SessionData) -> dict[str, int]:
        """Compact the persisted history now (the ``/compact`` command).

        Normal turns only compact when the budget demands it (R3b); this is the
        explicit user request, so it forces one page and **replaces** the stored
        history — L3 persisted outputs, the session file and memory remain as the
        safety nets (docs/r5-analysis.md 3.3).
        """
        budget = float(getattr(self.settings, "turn_timeout_seconds", 0.0) or 60.0)
        report = await self.compactor.compact_now(session, timeout=budget)
        if report["after"] != report["messages"]:
            self.closer.save(session)
        return report

    # ---- undo / redo (r17/T1) ----------------------------------------------

    @staticmethod
    def _turn_key(session: SessionData, user_turns_before: int) -> str:
        """``<session_id>/<n>`` — the one spelling capture and restore share."""
        return f"{session.session_id}/{user_turns_before + 1}"

    def _extract_image_mentions(self, text: str) -> tuple[ImageRef, ...]:
        """``@shot.png`` tokens that name a real image file under the workspace.

        These ride as message parts (r17/T5), not as injected text: the bytes
        are what the model needs, and ``<referenced-files>`` skips them.
        """
        from ai_coding.skills.registry import iter_mentions

        root = Path(self.workspace).expanduser().resolve()
        refs: list[ImageRef] = []
        seen: set[str] = set()
        for token in iter_mentions(text):
            mime = IMAGE_MIME_BY_SUFFIX.get(Path(token).suffix.lower())
            if mime is None or token in seen:
                continue
            try:
                resolved = (root / token).resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                seen.add(token)
                # r19/F3: the size is taken here, where the file was resolved
                # anyway, so the context budget never has to stat it.
                refs.append(
                    ImageRef(
                        path=token,
                        mime=mime,
                        name=Path(token).name,
                        bytes=resolved.stat().st_size,
                    )
                )
        return tuple(refs)

    def _load_image(self, ref: ImageRef) -> str | None:
        """The image loader handed to the engine (r17/T5).

        Every gate on the bytes lives in ``attachments.load_image`` (r18/F2) --
        this stays a named injection point, not a second implementation.
        """
        return load_image(self.workspace, ref)

    def last_undo_for(self, session_id: str) -> dict[str, Any] | None:
        """What ``/undo`` last reported *for this session*, for the IPC layer."""
        return self.last_undo.get(session_id)

    async def _forget_redo(self, session: SessionData) -> None:
        """Commit a staged revert: a new turn ends the one before it.

        OpenCode's rule (docs/snapshots): "Sending a new prompt while an undo is
        staged commits the revert."  Without it, /redo after a fresh turn replays
        a stale tail on top of live history and rolls the worktree back under it.
        """
        # r20/F2: the staged revert is one thing spread over three holders (plus
        # one durable copy).  If a new turn cleared only the messages, the next
        # /redo would still find a counter, a digest ring and file contents to
        # restore -- a baseline from a run the user already moved past.
        # L5: committing must be durable, not memory-only -- a crash before the
        # next save would otherwise resurrect that moved-past run on restart.
        # The save runs only when something was actually staged, so clean turns
        # pay nothing (the per-turn save rhythm stays untouched).
        had_staged = (
            self._undo_tail.pop(session.session_id, None) is not None
            or self._undo_state.pop(session.session_id, None) is not None
            or self._undo_files.pop(session.session_id, None) is not None
            or self.last_undo.pop(session.session_id, None) is not None
            or session.undo_state is not None
        )
        # P4: the staged revert is durable now, so committing it must clear the
        # session copy and the sidecar too -- otherwise a later /redo would
        # resurrect a run the user already moved past.
        session.undo_state = None
        self._undo_files_store.clear(session.session_id)
        if had_staged:
            self.closer.save(session)
        if self.snapshots is not None:
            try:
                await self.snapshots.forget(f"{session.session_id}/redo")
            except (OSError, TimeoutError, ValueError):
                pass  # best effort: a stale ref costs one undo target, not a turn

    async def _capture_snapshot(self, session: SessionData) -> None:
        """Pin the pre-turn workspace state under refs/ai-coding/undo.

        The key is ``turn_count`` -- the session's own counter, already the truth
        source for ``source_turn`` and the working-memory header (r18/F3).  It was
        previously "how many user messages exist", which counts the harness's
        own nudges and so missed the snapshot whenever a turn injected one.

        Best effort by design: undo is a safety net, not a turn prerequisite, so
        a missing git (or a git that overran its timeout) must not fail the turn.
        """
        await self._forget_redo(session)
        if self.snapshots is None:
            return
        try:
            await self.snapshots.capture(self._turn_key(session, session.turn_count))
            await self.snapshots.prune(session.session_id)
        except (OSError, TimeoutError, ValueError):
            pass

    def _hydrate_undo(self, session: SessionData) -> None:
        """Rebuild the process-local undo state from the session (P4).

        ``/undo`` writes the staged revert onto the session so ``/redo``
        outlives the process; these dicts are only its cache.  A live run that
        already holds them is left alone -- a second undo widens the staged
        revert, and re-reading the trimmed session would lose that.
        """
        sid = session.session_id
        if self._undo_tail.get(sid) is not None:
            return
        try:
            staged = session.staged_undo()
        except Exception:  # noqa: BLE001 -- L4: a hand-tampered undoState must
            # not break redo with a traceback.  Nothing staged is recoverable
            # from a corrupt record, so this is nothing to redo -- same verdict
            # as an empty stage, debug-logged like the memory harvest's
            # best-effort failures.
            _log.debug("staged undo unreadable", exc_info=True)
            return
        if staged is None:
            return
        self._undo_tail[sid] = list(staged.removed)
        self._undo_state[sid] = _Staged(
            turns=staged.turns,
            turn_count=staged.turn_count,
            digests=staged.digests,
        )

    async def undo_last_turn(self, session: SessionData) -> dict[str, Any]:
        """Roll the last user turn back: files to the snapshot, history to before it.

        Refused while a turn is running rather than queued behind it: the client
        gets an answer either way, and OpenCode makes the same call
        (``assertNotBusy``).  The text is handed back so the client can put it in
        the composer.  ``redo_last_turn`` is the inverse, held in process memory.

        ``reason`` says why no file moved — "nothing changed" and "this is not a
        git repository" must not look alike to the user (the r17 defect).
        """
        if self._turn_lock.locked():
            return {"ok": False, "reason": "a turn is running"}
        index = next(
            (
                i
                for i in range(len(session.messages) - 1, -1, -1)
                if session.messages[i].role == "user"
                and not session.messages[i].synthetic
            ),
            None,
        )
        if index is None:
            return {"ok": False, "reason": "nothing to undo"}
        text = session.messages[index].content or ""
        # What this turn wrote, as its tools reported it -- read before the
        # truncation below takes the messages away (r18/F4).
        created = {
            artifact.path
            for message in session.messages[index:]
            for artifact in message.artifacts
        }
        # One reason for both ways a snapshot can be absent -- not a git
        # repository, or the ref write failed.  capture() cannot tell them apart
        # and inventing a distinction the code cannot support would be a lie.
        reason = "no-snapshot" if self.snapshots is None else ""
        restored: list[str] = []
        failed: list[str] = []
        restore_ok = True
        if self.snapshots is not None:
            key = self._turn_key(session, max(0, session.turn_count - 1))
            try:
                if await self.snapshots.exists(key):
                    # r20/F2: the baseline is the state from before the *first*
                    # undo of this run.  Re-shooting it here would capture the
                    # half-undone state the second undo is standing on, so /redo
                    # would land somewhere the user was never in.
                    if self._undo_state.get(session.session_id) is None:
                        await self.snapshots.capture(f"{session.session_id}/redo")
                    report = await self.snapshots.restore(key, created=created)
                    restored, failed = report.restored, report.failed
                    # r20/F9: the content of files the turn created, read before
                    # they were removed.  A redo restores the baseline commit,
                    # which cannot mention an untracked file -- without these
                    # bytes the file is simply gone after a redo.
                    if report.saved:
                        held = self._undo_files.setdefault(session.session_id, {})
                        held.update(report.saved)
                        # P4: the same bytes go to the sidecar, so a redo after
                        # a restart has something to write back.
                        self._undo_files_store.save(session.session_id, report.saved)
                    if failed:
                        # r20/F1: the history is still truncated.  The files that
                        # did come back cannot be un-restored, so keeping the
                        # messages would leave a state where the transcript and
                        # the tree disagree about the same turn.  Say which paths
                        # did not move instead of implying a clean rewind.
                        restore_ok = False
                        reason = "restore-partial: " + ", ".join(failed[:3]) + (
                            f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
                        )
                else:
                    reason = "no-snapshot"
            except (OSError, TimeoutError, ValueError) as exc:
                restore_ok = False
                restored = []
                reason = f"restore-failed: {exc}"
        self._undo_tail[session.session_id] = (
            list(session.messages[index:]) + self._undo_tail.get(session.session_id, [])
        )
        # r20/F2: a second /undo widens the staged revert instead of replacing
        # it, so this run's messages go in front of the ones already held.
        session.messages = list(session.messages[:index])
        # Taking the turn back includes the state that turn produced: the counter
        # (so the next message is this turn again, not the next one) and its
        # digest line (or the working memory would summarise a turn that is gone).
        # ``last_compacted_turn`` and ``turn_usages`` stay: the first is a fact
        # about what was rewritten, the second is money already spent.
        staged = self._undo_state.get(session.session_id)
        self._undo_state[session.session_id] = _Staged(
            turns=(staged.turns + 1) if staged else 1,
            # The counter and the digest ring are recorded as they stood before
            # the *first* undo and left alone after that.  Each undo only trims
            # them from the tail, so re-reading the trimmed list would duplicate
            # what survived and adding to the count would invent turns that never
            # ran.
            turn_count=staged.turn_count if staged else session.turn_count,
            digests=staged.digests if staged else tuple(session.turn_digests),
        )
        session.turn_count = max(0, session.turn_count - 1)
        session.turn_digests = [
            digest for digest in session.turn_digests if digest.turn <= session.turn_count
        ]
        # P4: the staged revert belongs to the session, not to this process.
        staged = self._undo_state[session.session_id]
        session.stage_undo(
            list(self._undo_tail[session.session_id]),
            turns=staged.turns,
            turn_count=staged.turn_count,
            digests=staged.digests,
        )
        self.closer.save(session)
        report = {
            "ok": restore_ok,
            "text": text,
            "files": restored,
            "reason": reason,
            "messages_before": index + len(self._undo_tail[session.session_id]),
            "messages_after": index,
        }
        self.last_undo[session.session_id] = report
        return report

    async def redo_last_turn(self, session: SessionData) -> dict[str, Any]:
        """Put back the turn ``undo_last_turn`` removed (files and history)."""
        if self._turn_lock.locked():
            return {"ok": False, "reason": "a turn is running"}
        # P4: a fresh process has no process-local undo state, so rebuild it
        # from the session before reading anything.
        self._hydrate_undo(session)
        tail = self._undo_tail.get(session.session_id)
        if not tail:
            return {"ok": False, "reason": "nothing to redo"}
        reason = ""
        restored: list[str] = []
        failed: list[str] = []
        restore_ok = True
        session_id = session.session_id
        if self.snapshots is not None:
            try:
                key = f"{session_id}/redo"
                if await self.snapshots.exists(key):
                    report = await self.snapshots.restore(key)
                    restored, failed = report.restored, report.failed
                    if failed:
                        restore_ok = False
                        reason = "restore-partial: " + ", ".join(failed[:3]) + (
                            f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
                        )
                else:
                    reason = "no-snapshot"
            except (OSError, TimeoutError, ValueError) as exc:
                # r17 reported success here whatever happened, so the TUI said
                # "restored" for a restore that never ran.  restore_ok keeps the
                # same door undo uses, so an exception and a partial restore are
                # both a failure and no-snapshot is the one benign path (M-A3).
                restore_ok = False
                reason = f"restore-failed: {exc}"
        # r20/F9: the baseline commit is tracked files only, so the created ones
        # are written back from what undo kept.  A path that cannot be written
        # is named rather than swallowed -- it is the same "redo looked fine"
        # lie r20/F1 removed from the other half.
        held = self._undo_files.pop(session_id, None)
        if held is None:
            # P4: the bytes may only exist in the sidecar (an earlier process
            # read them before undo removed the files).
            held = self._undo_files_store.load(session_id)
        for rel in sorted(held):
            target = Path(self.workspace) / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(held[rel])
            except OSError:
                failed.append(rel)
            else:
                restored.append(rel)
        if failed:
            restore_ok = False
            reason = "restore-partial: " + ", ".join(failed[:3]) + (
                f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
            )
        session.messages = list(session.messages) + tail
        # r20/F2: restore the counter and the digest lines undo trimmed.  Without
        # them the turns would be back in the transcript but missing from the
        # working-memory ring, and the next turn would be numbered as if they
        # had never run.
        staged = self._undo_state.pop(session_id, None)
        if staged is not None:
            session.turn_count = staged.turn_count
            session.turn_digests = list(staged.digests)
        self._undo_tail.pop(session_id, None)
        self.last_undo.pop(session_id, None)
        # P4: the staged revert is finished; drop it from both stores.
        session.undo_state = None
        self._undo_files_store.clear(session_id)
        self.closer.save(session)
        return {
            "ok": restore_ok,
            "reason": reason,
            "files": sorted(set(restored)),
            "messages": len(session.messages),
        }

    # ---- request assembly -------------------------------------------------

    @staticmethod
    def _remaining(deadline: Deadline | None) -> float | None:
        """Seconds left in the turn, or ``None`` when there is no ceiling."""
        return remaining_of(deadline)

    async def _build_instructions(
        self, deadline: Deadline | None = None, session: Any = None
    ) -> str | None:
        """Assemble the **static** system prompt (memory is never part of it)."""
        if self.prompt is None:
            return None
        project_text = self.project.load() if self.project is not None else ""
        catalog = ""
        if self.skills is not None:
            current = self.skills.all()
            # E-5: a denied skill is not offered to the model at all.  The
            # gate withholds the call and the mention block withholds the
            # injection; this is the third face of the same boundary.
            denied = denied_skill_ids(
                self.skills, getattr(self.settings, "skill_permissions", None)
            )
            if denied:
                current = tuple(skill for skill in current if skill.id not in denied)
            catalog = build_catalog(current)
            # The catalog is *not* byte-stable within a session: `skill_review`
            # writes new skills every few turns. So say so when it changes —
            # otherwise the model reads a list that silently grew. A prompt only
            # grows, so a removal is stated as a negation.
            if session is not None:
                change = superseded_note(tuple(session.listed_skill_ids), current)
                if change:
                    catalog = f"{catalog}\n{change}" if catalog else change
                session.listed_skill_ids = tuple(skill.id for skill in current)
        # 18 §10.5 (P2): the prompt posture rides the one build call; the
        # session may be None on defensive paths — normal is the fail-safe.
        mode = "normal"
        readonly = False
        session_rules: tuple[Rule, ...] = ()
        agent_rules: tuple[Rule, ...] = ()
        if session is not None:
            mode = getattr(session, "mode", "normal") or "normal"
            readonly = bool(getattr(session, "readonly", False))
            session_rules = self._effective_session_rules(session)
            agent_rules = self._agent_rules(session)
        return self.prompt.build(
            project_instructions=project_text,
            skills_catalog=catalog,
            mode=mode,
            agent_instructions=self._agent_body(session) if session is not None else "",
            # C-2: the same fact the step picks its schemas from (the tool
            # surface), so the prompt never teaches a tool the request cannot
            # carry. One rule, read twice; no second surface to keep in sync.
            tools=self._prompt_tools(mode, readonly, session_rules, agent_rules),
        )

    def _prompt_tools(
        self,
        mode: str,
        readonly: bool,
        session_rules: tuple[Rule, ...] = (),
        agent_rules: tuple[Rule, ...] = (),
    ) -> tuple[str, ...]:
        """The tool names the methods section should speak for.

        Read-only drops the mutating tools, which is what makes the guidance
        shorter in plan mode rather than merely different -- and outside the
        planning posture the plan-only tools are gone, so the prompt cannot
        teach a tool the request does not carry.  A wholly denied action is
        gone for the same reason: the request does not carry it.
        """
        return tuple(
            tool.name
            for tool in self._surface_tools(
                mode, readonly, session_rules, agent_rules
            )
        )

    def _surface_tools(
        self,
        mode: str,
        readonly: bool,
        session_rules: tuple[Rule, ...] = (),
        agent_rules: tuple[Rule, ...] = (),
    ) -> list[BaseTool]:
        """The tools this request may call, in the registry's own order.

        Three predicates, all about what the request may do: the read-only
        posture drops the mutating tools (the dispatch gate refuses them
        structurally -- 只读探索批次, measured: 3-4 bash collisions per
        planning turn), outside the planning posture the plan-only tools are
        not advertised at all (N1), and a wholly denied action is not offered
        (M1a).  A scoped denial keeps the tool and refuses at dispatch, where
        the model can read which resource it hit.
        """
        tools = list(self.tools.all_tools)
        if mode != "plan":
            tools = [tool for tool in tools if not tool.plan_only]
        if readonly:
            # M3 T5: in the plan posture a mutating tool with a **scoped**
            # allow is reachable (the plan directory), so it must stay in the
            # catalogue -- otherwise the rule could never be reached.  Every
            # other posture keeps the old rule exactly: ``/readonly`` is an
            # absolute switch the operator threw, and a session rule must not
            # widen it.
            plan = mode == "plan"
            tools = [
                tool
                for tool in tools
                if not tool.mutates_world
                or (
                    plan
                    and self.gate.partially_allowed(
                        tool.name,
                        session_rules=session_rules,
                        agent_rules=agent_rules,
                        posture="plan",
                    )
                )
            ]
        posture = posture_of(mode, readonly)
        return [
            tool
            for tool in tools
            if not self.gate.hides(
                tool.name,
                session_rules=session_rules,
                agent_rules=agent_rules,
                posture=posture,
            )
        ]

    def _surface_schemas(self, session: Any) -> list[dict[str, Any]]:
        """The schema list for this request -- one per posture and rule set.

        The cache is what keeps the request byte-stable: the same posture and
        the same session rules always produce the same list, in the same
        order.  The rules are in the key because the catalogue now depends on
        them: ``/permissions deny edit *`` must change the very next request.
        """
        mode = str(getattr(session, "mode", "normal") or "normal")
        readonly = bool(getattr(session, "readonly", False))
        session_rules = self._effective_session_rules(session)
        agent_rules = self._agent_rules(session)
        key = (mode, readonly, session_rules, agent_rules)
        schemas = self._surface_cache.get(key)
        if schemas is None:
            schemas = tool_schemas_from_tools(
                self._surface_tools(mode, readonly, session_rules, agent_rules)
            )
            self._surface_cache[key] = schemas
        return schemas

    # ---- memory -----------------------------------------------------------

    # ---- persistence ------------------------------------------------------

    @property
    def last_outcome(self) -> TurnOutcome | None:
        """The last closed turn's outcome (the sub-agent reads this)."""
        return self.closer.last_outcome

    @property
    def last_artifacts(self) -> list[ToolArtifact]:
        """What this turn produced so far (kept as a name for the loop's API)."""
        return self.closer.artifacts

    # ---- events -----------------------------------------------------------

    def _emit(self, event: LoopEvent) -> None:
        self.observer.on_event(event)

    def _restate_engine_retry(
        self, *, kind: str, attempt: int, delay_seconds: float, reason: str
    ) -> None:
        """Engine-level retry -> loop event (M1).

        Called synchronously from inside the engine's retry loop, so it must stay
        trivial: an observer that raised here would surface as a provider error,
        which is exactly the wrong diagnosis for a UI problem.  ``_emit`` already
        funnels through ``SafeObserver``, and the transcript observer is the
        component that swallows presentation errors.
        """
        self._emit(
            ProviderRetried(
                kind=kind,
                attempt=attempt,
                delay_seconds=delay_seconds,
                # The exception text can be a whole HTTP body; the transcript
                # truncates too, but an unbounded string would be copied into
                # every recorder that keeps the event.
                reason=reason[:200],
            )
        )

    def _restate_engine_fallback(
        self, *, from_model: str, to_model: str, kind: str, reason: str
    ) -> None:
        """Engine chain switch -> loop event (M2).

        Synchronous, like the retry restatement above, so it has to stay
        trivial: it runs inside the engine's own step, where a raised exception
        would be read as a provider failure -- the one diagnosis that must never
        be caused by a UI bug.
        """
        self._emit(
            ProviderFallback(
                from_model=from_model,
                to_model=to_model,
                kind=kind,
                # A rejected key lives in here, so the trace is the right place
                # for the full text; the transcript truncates when rendering.
                reason=reason[:200],
            )
        )


__all__ = ["AgentLoop", "DeltaSink"]
