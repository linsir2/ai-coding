"""The self-owned ReAct loop (R1a-4, re-shaped in R5).

The loop is *ours*: it drives the iteration, decides when a turn is over, gates
the stop on the task list, executes tools through the dispatcher, and checkpoints
the session between iterations.  The engine only ever performs one model call.

R5 kept that shape and gave its parts names.  The loop body is still four moves,
but each one is a step you can point at:

    bind observer / trace, append the user message, reset guard state
    for each iteration:
        ask        -> _iteration_decision(): another round, given round/clock/spend?
        assemble   -> Compactor.compress() + _build_instructions()
                      + TurnMemory.block() + build_tail()          (the volatile tail)
                      + TurnReporter.report_context()
        model      -> _model_step(): one step, timed and reported
        act        -> _run_tools(): dispatch, write back, checkpoint
        (no tool calls) -> TurnPolicy.decide_after_step(): finish, or nudge
        close      -> _finalize() + TurnMemory.harvest() + turn.end hooks

Four things are deliberately asymmetric, and the R5 notes say why:

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

What stayed here on purpose: the iteration, the model call, tool dispatch and
persistence.  A loop you cannot read is not "ours" any more (docs/r5-analysis.md 2.2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
from ai_coding.core.compaction import Compactor
from ai_coding.core.dispatch import DispatchOutcome, ToolDispatcher
from ai_coding.core.events import (
    AssistantMessage,
    IterationStarted,
    LoopEvent,
    LoopObserver,
    MemoryInjected,
    NullObserver,
    TaskUpdated,
)
from ai_coding.core.guard import GuardChain, TurnView
from ai_coding.core.hooks import HookDecision, HookPoint
from ai_coding.core.memory_turn import TurnMemory
from ai_coding.core.request_builder import TailBlock, build_tail, tool_context
from ai_coding.core.trace import short_json
from ai_coding.core.turn_policy import (
    TaskInjection,
    TurnAction,
    TurnDecision,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.core.turn_report import TurnReporter
from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolArtifact, ToolOutput
from ai_coding.domain.run import assistant_message, tool_message, user_message
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import (
    append_digest,
    build_digest,
    render_handoff,
    render_working_memory,
)
from ai_coding.infra.deadline import (
    Deadline,
    DeadlineExpired,
    remaining_of,
    run_with_deadline,
)
from ai_coding.infra.time_utils import now_utc
from ai_coding.security.approval import ApprovalCallback
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.schema import tool_schemas

if TYPE_CHECKING:
    from ai_coding.ai.prompt import PromptAssembler
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.core.project_instructions import ProjectInstructionsLoader
    from ai_coding.memory.service import MemoryService
    from ai_coding.skills.registry import SkillRegistry

DeltaSink = Callable[[str], None]

_log = logging.getLogger(__name__)

LONG_SESSION_STRIDE = 3


class _SafeObserver:
    """Wraps an observer so a UI failure can never break a turn.

    Rendering is a best-effort side channel: if it explodes we log and carry on
    (docs/r1a-4-analysis.md §8 核验⑤).
    """

    def __init__(self, inner: LoopObserver) -> None:
        self._inner = inner
        # R4a: a renderer that keeps failing is still worth reporting once per
        # turn, instead of vanishing into a debug log (docs/r4-analysis.md 1.1).
        self.failures = 0

    def on_event(self, event: LoopEvent) -> None:
        try:
            self._inner.on_event(event)
        except Exception:  # noqa: BLE001 — UI failures must not kill the turn
            self.failures += 1
            _log.debug("loop observer failed on %s", type(event).__name__, exc_info=True)


def _terminal_outcome(
    decision: TurnDecision, *, budget_expired: bool, last_text: str | None, iterations: int
) -> TurnOutcome:
    """Translate "stop, but why" into the turn's terminal state (R2/R4b)."""
    if budget_expired:
        status = TurnStatus.TIMEOUT
    elif decision.reason == "turn token budget exhausted":
        status = TurnStatus.BUDGET
    else:
        status = TurnStatus.MAX_ITERATIONS
    return TurnOutcome(
        status=status, text=last_text, iterations=iterations, reason=decision.reason
    )


def _stopped_outcome(last_text: str | None, iterations: int) -> TurnOutcome:
    """The approval callback asked to stop; the turn ends where it stood."""
    return TurnOutcome(
        status=TurnStatus.STOPPED,
        text=last_text,
        iterations=iterations,
        reason="user stopped the turn",
    )


def _aborted_outcome(last_text: str | None, iterations: int) -> TurnOutcome:
    """The user interrupted the turn; the partial work is still worth keeping."""
    return TurnOutcome(
        status=TurnStatus.ABORTED, text=last_text, iterations=iterations, reason="cancelled"
    )


def _failed_outcome(
    error: Exception, *, last_text: str | None, iterations: int
) -> TurnOutcome:
    """A deadline or an engine failure — the two states that end a turn early."""
    if isinstance(error, DeadlineExpired):
        reason = (
            f"turn deadline exceeded: {error.label} was abandoned after "
            f"{error.timeout:g}s"
        )
        return TurnOutcome(
            status=TurnStatus.TIMEOUT, text=last_text, iterations=iterations, reason=reason
        )
    return TurnOutcome(
        status=TurnStatus.ERROR, text=None, iterations=iterations, reason=str(error)
    )


def _volatile(name: str, text: str | None) -> TailBlock | None:
    """A tail block, or ``None`` when there is nothing to inject."""
    if not text:
        return None
    return TailBlock(name=name, message=ChatMessage(role="user", content=text, timestamp=""))


def _content_of(message: ChatMessage | None) -> str | None:
    """The text of an optional injected message (``None`` when there is none)."""
    return message.content if message is not None else None


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
        context: ContextManager | None = None,
        memory: MemoryService | None = None,
        skills: SkillRegistry | None = None,
        project: ProjectInstructionsLoader | None = None,
        settings: AIConfig | None = None,
        observer: LoopObserver | None = None,
        workspace: str = ".",
        trace: Any | None = None,
        guards: GuardChain | None = None,
        hooks: Any | None = None,
    ) -> None:
        self.engine = engine
        self.sessions = sessions
        self.tools = tools
        self.prompt = prompt
        self.context = context
        self.memory = memory
        self.skills = skills
        self.project = project
        self.settings = settings or AIConfig()
        self.workspace = workspace
        self.observer: LoopObserver = _SafeObserver(observer or NullObserver())
        # R4a: an optional trace recorder.  It arrives as part of the observer
        # braid, and the loop only re-binds it to the current session (the CLI
        # builds one loop for a whole REPL, so the session can change under it).
        self.trace = trace
        # R4c: user hooks.  ``turn.start`` may inject context, which lands in the
        # volatile tail (never the system prompt, never persisted) so the cache
        # discipline from R3 still holds (docs/r4-analysis.md 6.4).
        self.hooks = hooks
        # Task injection + stop policy live in their own pure unit (R2c).
        self.policy = TurnPolicy(TurnPolicyConfig.from_ai_config(self.settings))
        self._tool_schemas = tool_schemas(tools)
        # R4a: the tool schemas are part of every prompt, so their size belongs in
        # the attribution (docs/r4-analysis.md 4.3).
        self._tools_text = short_json(self._tool_schemas)
        self._tools_chars = len(self._tools_text)
        self.dispatcher = ToolDispatcher(
            tools,
            gate or PermissionGate(),
            approval,
            max_parallel=self.settings.max_concurrent_tools,
            observer=self.observer,
            guards=guards,
            hooks=hooks,
        )
        # R5: everything this turn says about itself (events, ledger, degradation)
        # lives in one unit, so the loop body only has to choreograph.
        self.reporter = TurnReporter(tools_text=self._tools_text, emit=self._emit)
        # R5: the memory-facing half of a turn (block / harvest / status).
        self.turn_memory = TurnMemory(memory, emit=self._emit, save=self._save)
        # R5: history compaction (budget-driven for turns, forced for /compact).
        self.compactor = Compactor(context, emit=self._emit)
        self.last_outcome: TurnOutcome | None = None
        self.last_artifacts: list[ToolArtifact] = []

    # ---- the turn ---------------------------------------------------------

    async def process_input(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
    ) -> str:
        """Run one user turn to completion and return the final assistant text."""
        self._bind_observer(observer)
        self._bind_trace(session)
        session.add_message(user_message(user_input, now_utc()))
        # R6a: the session-level turn counter.  It is what `source_turn` and the
        # <working-memory> header mean by "turn N" (docs/r6-analysis.md 2.4).
        session.turn_count = int(getattr(session, "turn_count", 0) or 0) + 1
        turn_number = session.turn_count
        self.last_artifacts = []
        if session.task is None:
            session.task = TaskState(goal=user_input)
        task = session.task
        # R4b: guard state (duplicate calls, …) is per turn, never per process.
        self.dispatcher.guards.reset()
        self.reporter.begin_turn(self.observer)
        hook_context = await self._hook_context(session)

        policy = self.policy
        max_iterations = policy.config.max_iterations
        turn_timeout = float(getattr(self.settings, "turn_timeout_seconds", 0.0) or 0.0)
        deadline = Deadline.after(turn_timeout) if turn_timeout > 0 else None
        iterations = 0
        nudges = 0
        last_text: str | None = None
        summaries: list[str] = []
        memory_announced = False
        # R4a: the turn's ledger and the previous request's canonical form, which
        # together answer "what did this cost" and "did the prefix move".
        ledger = self.reporter.ledger
        previous_request: str | None = None
        outcome = TurnOutcome(
            status=TurnStatus.MAX_ITERATIONS, reason="iteration budget exhausted"
        )

        try:
            index = 0
            while True:
                index += 1
                budget_expired = deadline is not None and deadline.expired
                decision = self._iteration_decision(
                    index=index, nudges=nudges, ledger=ledger, budget_expired=budget_expired
                )
                if decision.action is TurnAction.FINISH:
                    outcome = _terminal_outcome(
                        decision,
                        budget_expired=budget_expired,
                        last_text=last_text,
                        iterations=iterations,
                    )
                    break

                iterations = index
                self._emit(IterationStarted(index=index, max_iterations=max_iterations))

                history = await self.compactor.compress(
                    session.messages, deadline=deadline, on_summary=summaries.append
                )
                # R6a: the ring only pays for itself when this request's history was
                # actually rewritten — otherwise the recent turns are still there
                # (docs/r6-analysis.md 2.1, D-R6-2).
                compacted = Compactor.did_compact(session.messages, history)
                instructions = await self._build_instructions(history, deadline)
                injection: TaskInjection = policy.inject_task(task, instructions)
                instructions = injection.instructions
                # Memory and task state are *volatile tail blocks*: the system
                # prompt stays byte-identical and the history stays append-only, so
                # only this small region diverges between turns (docs/r2 §1.13).
                memory_block, memory_ids = self.turn_memory.block()
                if memory_block and not memory_announced:
                    self._emit(
                        MemoryInjected(note_ids=memory_ids, chars=len(memory_block))
                    )
                    memory_announced = True
                digest_block = render_working_memory(
                    session.turn_digests,
                    current_turn=turn_number,
                    max_chars=int(getattr(self.settings, "working_memory_chars", 1200)),
                    compacted=compacted,
                )
                tail, tail_blocks = build_tail(
                    [
                        _volatile("memory", memory_block),
                        _volatile("task", _content_of(injection.message)),
                        _volatile("hook", hook_context),
                        _volatile("digest", digest_block),
                    ]
                )
                request = [*history, *tail]
                previous_request = self.reporter.report_context(
                    iteration=index,
                    instructions=instructions,
                    history=history,
                    request=request,
                    tail_blocks=tail_blocks,
                    previous=previous_request,
                )

                # The one wait that cannot degrade: a model step either answers or
                # the turn ends (docs/r2-analysis.md §7.3).
                step = await self._model_step(
                    session=session,
                    request=request,
                    instructions=instructions,
                    deadline=deadline,
                    on_delta=on_delta,
                    index=index,
                )
                last_text = step.text

                if not step.tool_calls:
                    decision = policy.decide_after_step(
                        TurnSnapshot(
                            iteration=index,
                            nudges=nudges,
                            open_items=tuple(task.open_items()),
                            final_text=step.text,
                        )
                    )
                    if decision.action is TurnAction.FINISH:
                        outcome = TurnOutcome(
                            status=TurnStatus.COMPLETED,
                            text=step.text,
                            iterations=index,
                            reason=decision.reason,
                        )
                        break
                    nudges += 1
                    session.add_message(
                        user_message(decision.inject or "", now_utc())
                    )
                    self._checkpoint(session, index)
                    continue

                struck = await self._run_tools(
                    step,
                    session=session,
                    task=task,
                    history=history,
                    instructions=instructions,
                    deadline=deadline,
                    index=index,
                )
                if struck:
                    outcome = _stopped_outcome(step.text, index)
                    break
        except asyncio.CancelledError:
            # Record what happened, persist the partial transcript, then let the
            # cancellation propagate — swallowing it is an asyncio anti-pattern.
            self._close_turn(
                session,
                user_input=user_input,
                task=task,
                turn_number=turn_number,
                outcome=_aborted_outcome(last_text, iterations),
            )
            raise
        except (DeadlineExpired, EngineError) as exc:
            outcome = _failed_outcome(exc, last_text=last_text, iterations=iterations)

        outcome = self._close_turn(
            session,
            user_input=user_input,
            task=task,
            turn_number=turn_number,
            outcome=outcome,
        )
        await self.turn_memory.harvest(
            session, deadline=deadline, summaries=summaries, outcome=outcome
        )
        await self._fire_hooks(HookPoint.TURN_END, session)
        # R6a: a turn that ran out of budget used to hand the user an empty string.
        return outcome.text or outcome.handoff

    def _bind_observer(self, observer: LoopObserver | None) -> None:
        """Honour a per-call observer (the REPL swaps the UI between turns)."""
        if observer is not None:
            self.observer = _SafeObserver(observer)
            self.dispatcher.observer = self.observer

    def _bind_trace(self, session: SessionData) -> None:
        """Point the trace at the current session (full capture reads it)."""
        if self.trace is None:
            return
        self.trace.session_id = session.session_id
        self.trace.attach_session(session, start=len(session.messages))

    def _trace_path(self, session: SessionData) -> str:
        """Where this turn's trace lives, for the handoff (best effort)."""
        path = getattr(self.trace, "current_path", None)
        return str(path) if path else ""

    def _iteration_decision(
        self,
        *,
        index: int,
        nudges: int,
        ledger: Any,
        budget_expired: bool,
    ) -> TurnDecision:
        """May this iteration start, given the round, the clock and the spend?"""
        return self.policy.decide_next_iteration(
            TurnSnapshot(
                iteration=index,
                nudges=nudges,
                tokens_spent=ledger.totals.total_tokens,
            ),
            budget_expired=budget_expired,
        )

    async def _run_tools(
        self,
        step: StepResult,
        *,
        session: SessionData,
        task: TaskState,
        history: list[ChatMessage],
        instructions: str | None,
        deadline: Deadline | None,
        index: int,
    ) -> bool:
        """Run this step's tool calls; return whether the user stopped the turn.

        Dispatch, write-back, the todo event and the mid-turn checkpoint all
        happen here, so the loop body reads as the ReAct cycle it is.
        """
        outcomes = await self.dispatcher.dispatch_batch(
            step.tool_calls,
            tool_context(
                task=task,
                workspace=self.workspace,
                session=session,
                history=history,
                instructions=instructions,
                emit=self._emit,
            ),
            deadline=deadline,
            iteration=index,
            view=TurnView(
                iteration=index,
                tokens_spent=self.reporter.tokens_spent(),
                task_open=len(task.open_items()),
            ),
        )
        self._write_tool_messages(session, outcomes)
        if any(item.call.name == "todo_write" for item in outcomes):
            self._emit(TaskUpdated(task=task))
        self._checkpoint(session, index)
        return any(item.halted for item in outcomes)

    async def _model_step(
        self,
        *,
        session: SessionData,
        request: list[ChatMessage],
        instructions: str | None,
        deadline: Deadline | None,
        on_delta: DeltaSink | None,
        index: int,
    ) -> StepResult:
        """One model call — the only wait that cannot degrade (docs/r2 §7.3).

        It is timed, reported and appended here so the loop body reads as
        "decide → ask → act" without the bookkeeping in between.
        """
        started = time.monotonic()
        step = await run_with_deadline(
            self.engine.step(
                request,
                tools=self._tool_schemas,
                instructions=instructions,
                on_delta=on_delta,
            ),
            timeout=self._remaining(deadline),
            label="model_step",
        )
        self.reporter.report_step(
            index=index, step=step, latency_ms=int((time.monotonic() - started) * 1000)
        )
        self._emit(AssistantMessage(text=step.text, tool_calls=tuple(step.tool_calls)))
        session.add_message(assistant_message(step, now_utc()))
        return step

    async def aclose(self) -> None:
        """Release engine resources (the loop owns nothing else)."""
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
            self._save(session)
        return report

    # ---- request assembly -------------------------------------------------

    async def _hook_context(self, session: SessionData) -> str:
        """Ask ``turn.start`` hooks whether they want to add anything.

        The result becomes a volatile tail block: it changes every turn, but only
        inside the small region that already changes, so the cached prefix of the
        conversation survives (docs/r4-analysis.md 6.4).
        """
        if self.hooks is None:
            return ""
        try:
            results = await self.hooks.run(
                HookPoint.TURN_START,
                {
                    "session": session.session_id,
                    "workspace": self.workspace,
                    "messages": len(session.messages),
                },
            )
        except Exception:  # noqa: BLE001 — hooks must never break a turn
            _log.debug("turn.start hooks failed", exc_info=True)
            return ""
        parts = [
            result.context
            for result in results
            if result.decision is HookDecision.INJECT and result.context
        ]
        if not parts:
            return ""
        return "<hook-context>\n" + "\n".join(parts) + "\n</hook-context>"

    async def _fire_hooks(self, point: HookPoint, session: SessionData) -> None:
        """Fire an observation-only hook point (never affects the turn)."""
        if self.hooks is None:
            return
        try:
            await self.hooks.run(
                point, {"session": session.session_id, "workspace": self.workspace}
            )
        except Exception:  # noqa: BLE001 — hooks must never break a turn
            _log.debug("hook point %s failed", point.value, exc_info=True)

    @staticmethod
    def _remaining(deadline: Deadline | None) -> float | None:
        """Seconds left in the turn, or ``None`` when there is no ceiling."""
        return remaining_of(deadline)

    async def _build_instructions(
        self, history: list[ChatMessage], deadline: Deadline | None = None
    ) -> str | None:
        """Assemble the **static** system prompt (memory is never part of it)."""
        if self.prompt is None:
            return None
        project_text = self.project.load() if self.project is not None else ""
        skills_catalog = self.skills.catalog_text() if self.skills is not None else ""
        return self.prompt.build(
            project_instructions=project_text,
            skills_catalog=skills_catalog,
        )

    # ---- memory -----------------------------------------------------------

    # ---- persistence ------------------------------------------------------

    def _write_tool_messages(
        self, session: SessionData, outcomes: list[DispatchOutcome]
    ) -> None:
        for item in outcomes:
            self.last_artifacts.extend(item.result.artifacts)
            session.add_message(
                tool_message(
                    ToolOutput(
                        call_id=item.call.id,
                        tool_name=item.call.name,
                        output=item.result.output,
                        success=item.result.success,
                        data=item.result.data,
                        artifacts=item.result.artifacts,
                    ),
                    now_utc(),
                )
            )

    def _checkpoint(self, session: SessionData, index: int) -> None:
        """Persist mid-turn, throttled once the session has grown large."""
        limit = int(self.settings.persist_checkpoint_messages)
        if len(session.messages) <= limit or index % LONG_SESSION_STRIDE == 0:
            self._save(session)

    def _finalize(self, session: SessionData, outcome: TurnOutcome) -> None:
        """Persist, then close the turn through the reporter (order matters)."""
        self._save(session)
        self.last_outcome = self.reporter.finish_turn(outcome, self.observer)

    def _close_turn(
        self,
        session: SessionData,
        *,
        user_input: str,
        task: TaskState,
        turn_number: int,
        outcome: TurnOutcome,
    ) -> TurnOutcome:
        """Record what this turn leaves behind, then persist it (R6a).

        Two things happen here, in this order, because both have to be in the saved
        session: the **handoff** (what to tell the user when the model ran out of
        turn without a final word) and the **digest** (the one line the next
        request's ``<working-memory>`` block is rendered from).
        ``TurnOutcome.text`` is never touched — the handoff is harness-authored, and
        callers must still be able to tell whether the model itself delivered
        (docs/r6-analysis.md 2.1, D-R6-4).
        """
        if not outcome.text:
            handoff = render_handoff(
                outcome=outcome,
                task=task,
                artifacts=self.last_artifacts,
                trace_path=self._trace_path(session),
            )
            if handoff:
                outcome = replace(outcome, handoff=handoff)
        append_digest(
            session,
            build_digest(
                turn=turn_number,
                user_input=user_input,
                outcome=outcome,
                artifacts=self.last_artifacts,
                task=task,
            ),
            keep=int(getattr(self.settings, "working_memory_keep", 12)),
        )
        self._finalize(session, outcome)
        return outcome

    def _save(self, session: SessionData) -> None:
        session.ensure_tool_pairing()
        try:
            self.sessions.save(session)
        except OSError:
            # Persistence is best-effort, but a silent failure used to be
            # invisible; R4a at least says so (docs/r4-analysis.md 1.1).
            _log.debug("session save failed", exc_info=True)
            self.reporter.report_degraded(
                "persist",
                "the session file could not be written",
                "the transcript stays in memory only",
            )

    # ---- events -----------------------------------------------------------

    def _emit(self, event: LoopEvent) -> None:
        self.observer.on_event(event)


__all__ = ["AgentLoop", "DeltaSink"]
