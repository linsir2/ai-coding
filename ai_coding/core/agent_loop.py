"""The self-owned ReAct loop (R1a-4).

The loop is *ours*: it drives the iteration, decides when a turn is over, gates
the stop on the task list, executes tools through the dispatcher, and checkpoints
the session between iterations.  The engine only ever performs one model call.

Turn shape::

    append user message
    task = session.task or TaskState(goal=user_input)
    for index in 1..max_tool_iterations:
        compact       = context.compress(session.messages)   # a copy, never written back
        instructions  = prompt.build(project, skills, memory)
        request       = compact + volatile <task-state> block
        step          = engine.step(request, tools, instructions, on_delta)
        append assistant
        no tool calls -> stop policy (todo gate) or finish
        tool calls    -> dispatcher.dispatch_batch(...) -> append tool messages -> checkpoint

Two things are deliberately asymmetric (docs/r1a-4-analysis.md D1):
the ``<task-state>`` block is **volatile** (regenerated every request, never
persisted), while the ``[task-gate]`` nudge is **persisted** — it is a real
instruction that changed the model's behaviour, so replaying the transcript
without it would break the conversation's meaning.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
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
from ai_coding.core.trace import short_json
from ai_coding.core.turn_policy import (
    TaskInjection,
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.core.turn_report import TurnReporter
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolArtifact, ToolOutput
from ai_coding.domain.run import assistant_message, tool_message, user_message
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
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
from ai_coding.tools.base import ToolContext
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
        if observer is not None:
            self.observer = _SafeObserver(observer)
            self.dispatcher.observer = self.observer
        if self.trace is not None:
            self.trace.session_id = session.session_id
            # R4a: full capture reads the session (the only place full text lives).
            self.trace.attach_session(session, start=len(session.messages))

        session.add_message(user_message(user_input, now_utc()))
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
                decision = policy.decide_next_iteration(
                    TurnSnapshot(
                        iteration=index,
                        nudges=nudges,
                        tokens_spent=ledger.totals.total_tokens,
                    ),
                    budget_expired=budget_expired,
                )
                if decision.action is TurnAction.FINISH:
                    expired = budget_expired or decision.reason == (
                        "turn token budget exhausted"
                    )
                    status = (
                        TurnStatus.TIMEOUT
                        if budget_expired
                        else TurnStatus.BUDGET
                        if expired
                        else TurnStatus.MAX_ITERATIONS
                    )
                    outcome = TurnOutcome(
                        status=status,
                        text=last_text,
                        iterations=iterations,
                        reason=decision.reason,
                    )
                    break

                iterations = index
                self._emit(IterationStarted(index=index, max_iterations=max_iterations))

                history = session.messages
                if self.context is not None:
                    history = await self._compress(
                        history, deadline, on_summary=summaries.append
                    )
                instructions = await self._build_instructions(history, deadline)
                injection: TaskInjection = policy.inject_task(task, instructions)
                instructions = injection.instructions
                # Memory and task state are *volatile tail blocks*: the system
                # prompt stays byte-identical and the history stays append-only, so
                # only this small region diverges between turns (docs/r2 §1.13).
                tail: list[ChatMessage] = []
                tail_blocks: list[tuple[str, int]] = []
                memory_block, memory_ids = self.turn_memory.block()
                if memory_block:
                    tail.append(
                        ChatMessage(role="user", content=memory_block, timestamp="")
                    )
                    tail_blocks.append(("memory", len(memory_block)))
                    if not memory_announced:
                        self._emit(
                            MemoryInjected(
                                note_ids=memory_ids, chars=len(memory_block)
                            )
                        )
                        memory_announced = True
                if injection.message is not None:
                    tail.append(injection.message)
                    tail_blocks.append(("task", len(injection.message.content or "")))
                if hook_context:
                    tail.append(
                        ChatMessage(role="user", content=hook_context, timestamp="")
                    )
                    tail_blocks.append(("hook", len(hook_context)))
                request = [*history, *tail]
                previous_request = self.reporter.report_context(
                    iteration=index,
                    instructions=instructions,
                    history=history,
                    request=request,
                    tail_blocks=tuple(tail_blocks),
                    previous=previous_request,
                )

                # The one wait that cannot degrade: a model step either answers or
                # the turn ends (docs/r2-analysis.md §7.3).
                step_started = time.monotonic()
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
                latency_ms = int((time.monotonic() - step_started) * 1000)
                last_text = step.text
                self.reporter.report_step(index=index, step=step, latency_ms=latency_ms)
                self._emit(
                    AssistantMessage(text=step.text, tool_calls=tuple(step.tool_calls))
                )
                session.add_message(assistant_message(step, now_utc()))

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

                outcomes = await self.dispatcher.dispatch_batch(
                    step.tool_calls,
                    self._tool_context(session, task, history, instructions),
                    deadline=deadline,
                    iteration=index,
                    view=TurnView(
                        iteration=index,
                        tokens_spent=ledger.totals.total_tokens,
                        task_open=len(task.open_items()),
                    ),
                )
                self._write_tool_messages(session, outcomes)
                if any(item.call.name == "todo_write" for item in outcomes):
                    self._emit(TaskUpdated(task=task))
                self._checkpoint(session, index)

                if any(item.halted for item in outcomes):
                    outcome = TurnOutcome(
                        status=TurnStatus.STOPPED,
                        text=step.text,
                        iterations=index,
                        reason="user stopped the turn",
                    )
                    break
        except asyncio.CancelledError:
            # Record what happened, persist the partial transcript, then let the
            # cancellation propagate — swallowing it is an asyncio anti-pattern.
            outcome = TurnOutcome(
                status=TurnStatus.ABORTED,
                text=last_text,
                iterations=iterations,
                reason="cancelled",
            )
            self._finalize(session, outcome)
            raise
        except DeadlineExpired as exc:
            outcome = TurnOutcome(
                status=TurnStatus.TIMEOUT,
                text=last_text,
                iterations=iterations,
                reason=(
                    f"turn deadline exceeded: {exc.label} was abandoned after "
                    f"{exc.timeout:g}s"
                ),
            )
        except EngineError as exc:
            outcome = TurnOutcome(
                status=TurnStatus.ERROR,
                text=None,
                iterations=iterations,
                reason=str(exc),
            )

        self._finalize(session, outcome)
        await self.turn_memory.harvest(
            session, deadline=deadline, summaries=summaries, outcome=outcome
        )
        await self._fire_hooks(HookPoint.TURN_END, session)
        return outcome.text or ""

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
        history with the compacted view — L3 persisted outputs, the session file
        and memory remain as the safety nets.
        """
        before = len(session.messages)
        report = {"messages": before, "after": before, "removed": 0}
        if self.context is None or before == 0:
            return report
        compact = getattr(self.context, "compress_if_needed", None)
        budget = float(getattr(self.settings, "turn_timeout_seconds", 0.0) or 60.0)
        try:
            if compact is not None:
                result = await run_with_deadline(
                    compact(list(session.messages), force=True),
                    timeout=budget if budget > 0 else 60.0,
                    label="compact",
                )
            else:
                result = await run_with_deadline(
                    self.context.compress(list(session.messages)),
                    timeout=budget if budget > 0 else 60.0,
                    label="compact",
                )
        except (DeadlineExpired, Exception):  # noqa: BLE001 — never break the REPL
            return report
        session.messages = list(result)
        self._save(session)
        after = len(session.messages)
        return {"messages": before, "after": after, "removed": before - after}

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

    async def _compress(
        self,
        history: list[ChatMessage],
        deadline: Deadline | None = None,
        *,
        on_summary: Callable[[str], None] | None = None,
    ) -> list[ChatMessage]:
        """Compact only when the budget says so; degrade to the raw history."""
        if self.context is None:
            return history
        # ``compress_if_needed`` is the R3b policy layer; older injected context
        # objects only implement ``compress`` (duck-typed, keeps them working).
        compact = getattr(self.context, "compress_if_needed", None)
        try:
            if compact is not None:
                return await run_with_deadline(
                    compact(history, on_summary=on_summary),
                    timeout=self._remaining(deadline),
                    label="context_compress",
                )
            return await run_with_deadline(
                self.context.compress(history),
                timeout=self._remaining(deadline),
                label="context_compress",
            )
        except DeadlineExpired:
            _log.debug("context compression ran out of budget; using the raw history")
            self.reporter.report_degraded(
                "context_compress",
                "compression exceeded its budget",
                "used the uncompressed history",
            )
            return history

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

    def _tool_context(
        self,
        session: SessionData,
        task: TaskState,
        history: list[ChatMessage],
        instructions: str | None,
    ) -> ToolContext:
        return ToolContext(
            task=task,
            workspace=self.workspace,
            session_id=session.session_id,
            messages=tuple(history),
            instructions=instructions,
            emit=self._emit,
        )

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
