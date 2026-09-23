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
    MemoryHarvested,
    MemoryInjected,
    NullObserver,
    TaskUpdated,
    TurnFinished,
)
from ai_coding.core.turn_policy import (
    TaskInjection,
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolArtifact, ToolOutput
from ai_coding.domain.run import assistant_message, tool_message, user_message
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.infra.deadline import Deadline, DeadlineExpired, run_with_deadline
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

    def on_event(self, event: LoopEvent) -> None:
        try:
            self._inner.on_event(event)
        except Exception:  # noqa: BLE001 — UI failures must not kill the turn
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
        # Task injection + stop policy live in their own pure unit (R2c).
        self.policy = TurnPolicy(TurnPolicyConfig.from_ai_config(self.settings))
        self._tool_schemas = tool_schemas(tools)
        self.dispatcher = ToolDispatcher(
            tools,
            gate or PermissionGate(),
            approval,
            max_parallel=self.settings.max_concurrent_tools,
            observer=self.observer,
        )
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

        session.add_message(user_message(user_input, now_utc()))
        self.last_artifacts = []
        if session.task is None:
            session.task = TaskState(goal=user_input)
        task = session.task

        policy = self.policy
        max_iterations = policy.config.max_iterations
        turn_timeout = float(getattr(self.settings, "turn_timeout_seconds", 0.0) or 0.0)
        deadline = Deadline.after(turn_timeout) if turn_timeout > 0 else None
        iterations = 0
        nudges = 0
        last_text: str | None = None
        summaries: list[str] = []
        memory_announced = False
        outcome = TurnOutcome(
            status=TurnStatus.MAX_ITERATIONS, reason="iteration budget exhausted"
        )

        try:
            index = 0
            while True:
                index += 1
                budget_expired = deadline is not None and deadline.expired
                decision = policy.decide_next_iteration(
                    TurnSnapshot(iteration=index, nudges=nudges),
                    budget_expired=budget_expired,
                )
                if decision.action is TurnAction.FINISH:
                    outcome = TurnOutcome(
                        status=(
                            TurnStatus.TIMEOUT if budget_expired else TurnStatus.MAX_ITERATIONS
                        ),
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
                memory_block, memory_ids = self._memory_block()
                if memory_block:
                    tail.append(
                        ChatMessage(role="user", content=memory_block, timestamp="")
                    )
                    if not memory_announced:
                        self._emit(
                            MemoryInjected(
                                note_ids=memory_ids, chars=len(memory_block)
                            )
                        )
                        memory_announced = True
                if injection.message is not None:
                    tail.append(injection.message)
                request = [*history, *tail]

                # The one wait that cannot degrade: a model step either answers or
                # the turn ends (docs/r2-analysis.md §7.3).
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
                last_text = step.text
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
        await self._harvest(session, deadline, summaries, outcome)
        return outcome.text or ""

    async def aclose(self) -> None:
        """Release engine resources (the loop owns nothing else)."""
        await self.engine.aclose()

    def memory_status(self) -> dict[str, Any]:
        """Memory counts for the UI (``/memory`` / ``/status``); never raises."""
        if self.memory is None:
            return {"total": 0, "active": 0, "pinned": 0, "by_kind": {}, "enabled": False}
        try:
            return dict(self.memory.status())
        except Exception:  # noqa: BLE001 — memory is best-effort
            return {"total": 0, "active": 0, "pinned": 0, "by_kind": {}, "enabled": False}

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

    @staticmethod
    def _remaining(deadline: Deadline | None) -> float | None:
        """Seconds left in the turn, or ``None`` when there is no ceiling."""
        if deadline is None:
            return None
        return max(deadline.remaining(), 0.0)

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

    def _memory_block(self) -> tuple[str, tuple[str, ...]]:
        """The volatile constraint block for this request (best-effort)."""
        if self.memory is None:
            return "", ()
        try:
            notes = self.memory.index_notes()
            if not notes:
                return "", ()
            return self.memory.render_index_block(notes), tuple(n.id for n in notes)
        except Exception:  # noqa: BLE001 — memory must never break a turn
            _log.debug("memory index block failed", exc_info=True)
            return "", ()

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
        self._save(session)
        self._emit(TurnFinished(outcome=outcome))
        self.last_outcome = outcome

    def _save(self, session: SessionData) -> None:
        session.ensure_tool_pairing()
        try:
            self.sessions.save(session)
        except OSError:
            pass  # persistence is best-effort; never break the turn

    async def _harvest(
        self,
        session: SessionData,
        deadline: Deadline | None,
        summaries: list[str],
        outcome: TurnOutcome,
    ) -> None:
        """Reconcile **this turn's delta** into memory (best-effort, never raises).

        The cursor lives in the session so a fresh loop (the CLI builds one per
        turn) does not re-harvest the whole transcript — that is what produced 100
        junk notes in case 8 of docs/r3-analysis.md.
        """
        if self.memory is None:
            return
        start = int(getattr(session, "harvested_messages", 0) or 0)
        delta = session.messages[start:]
        if not delta:
            return
        summary = "\n".join(text for text in summaries if text) or None
        try:
            report = await run_with_deadline(
                self.memory.harvest(
                    delta,
                    turn_index=len(session.messages),
                    session_id=session.session_id,
                    summary=summary,
                ),
                timeout=self._remaining(deadline),
                label="memory_harvest",
            )
        except Exception:  # noqa: BLE001 — memory is best-effort
            _log.debug("memory harvest failed", exc_info=True)
            return
        session.harvested_messages = len(session.messages)
        self._save(session)
        if report.added or report.updated or report.superseded or report.skipped:
            self._emit(
                MemoryHarvested(
                    added=tuple(report.added),
                    updated=tuple(report.updated),
                    superseded=tuple(report.superseded),
                    skipped=tuple(report.skipped),
                )
            )
        if outcome.text is None:
            return  # nothing was concluded; no reason to wake consolidation
        try:
            await run_with_deadline(
                self.memory.consolidate(),
                timeout=self._remaining(deadline),
                label="memory_consolidate",
            )
        except Exception:  # noqa: BLE001 — memory is best-effort
            _log.debug("memory consolidation failed", exc_info=True)

    # ---- events -----------------------------------------------------------

    def _emit(self, event: LoopEvent) -> None:
        self.observer.on_event(event)


__all__ = ["AgentLoop", "DeltaSink"]
