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
from collections.abc import Callable
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
    NullObserver,
    TaskUpdated,
    ToolSurfaceWarning,
)
from ai_coding.core.guard import GuardChain, TurnView
from ai_coding.core.hooks import HookPoint
from ai_coding.core.memory_turn import TurnMemory
from ai_coding.core.observers import SafeObserver
from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.request_builder import (
    tool_context,
)
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
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
)
from ai_coding.core.turn_report import TurnReporter
from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolArtifact
from ai_coding.domain.run import assistant_message, user_message
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
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.schema import tool_schema, tool_schemas

if TYPE_CHECKING:
    from ai_coding.ai.prompt import PromptAssembler
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.core.project_instructions import ProjectInstructionsLoader
    from ai_coding.memory.service import MemoryService
    from ai_coding.skills.registry import SkillRegistry

DeltaSink = Callable[[str], None]

_log = logging.getLogger(__name__)


def _tools_by_origin(registry: ToolRegistry) -> dict[str, int]:
    """Serialised schema chars per origin, biggest first (R7e-3).

    Per *tool* rather than per group, so the numbers add up to the same schemas the
    request carries (the whole-array text differs only by its brackets and commas).
    """
    sizes: dict[str, int] = {}
    for tool in registry.all_tools:
        sizes[tool.origin] = sizes.get(tool.origin, 0) + len(short_json(tool_schema(tool)))
    return dict(sorted(sizes.items(), key=lambda item: (-item[1], item[0])))


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
        self.observer: LoopObserver = SafeObserver(observer or NullObserver())
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
        # Task injection + stop policy live in their own pure unit (R2c).
        self.policy = TurnPolicy(TurnPolicyConfig.from_ai_config(self.settings))
        self._tool_schemas = tool_schemas(tools)
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
            gate or PermissionGate(),
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
        )
        # R5: everything this turn says about itself (events, ledger, degradation)
        # lives in one unit, so the loop body only has to choreograph.
        self.reporter = TurnReporter(tools_text=self._tools_text, emit=self._emit)
        # R7a: what a turn leaves behind (write-back, checkpoint, handoff, digest).
        self.closer = TurnCloser(sessions, self.reporter, self.settings)
        # R5: the memory-facing half of a turn (block / harvest / status).
        self.turn_memory = TurnMemory(memory, emit=self._emit, save=self.closer.save)
        # R5: history compaction (budget-driven for turns, forced for /compact).
        self.compactor = Compactor(context, emit=self._emit)
        # R7a: one request per iteration, assembled in its own unit (the four
        # volatile blocks, in their declared order).
        self.assembler = RequestAssembler(
            compactor=self.compactor,
            policy=self.policy,
            memory=self.turn_memory,
            reporter=self.reporter,
            build_instructions=self._build_instructions,
            settings=self.settings,
            emit=self._emit,
        )

    # ---- the turn ---------------------------------------------------------

    async def process_input(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
    ) -> str:
        """Run one user turn to completion and return the final assistant text."""
        frame = await self._begin_turn(session, user_input, observer)
        task = frame.task
        # R7a: everything the turn counts about itself lives in one object, so a
        # new rule is a method on the books rather than a tenth local variable.
        books = TurnBooks()
        # R4a: the previous request's canonical form, which answers "did the prefix
        # move" (the ledger it compares against lives on the reporter).
        previous_request: str | None = None
        outcome = TurnOutcome(
            status=TurnStatus.MAX_ITERATIONS, reason="iteration budget exhausted"
        )

        try:
            index = 0
            while True:
                index += 1
                budget_expired = frame.deadline is not None and frame.deadline.expired
                decision = self.policy.decide_next_iteration(
                    books.snapshot(
                        iteration=index, tokens_spent=self.reporter.tokens_spent()
                    ),
                    budget_expired=budget_expired,
                )
                if decision.action is TurnAction.FINISH:
                    outcome = terminal_outcome(
                        decision,
                        budget_expired=budget_expired,
                        last_text=books.last_text,
                        iterations=books.iterations,
                    )
                    break
                if decision.action is TurnAction.NUDGE:
                    # R6b tier 2.  Like the [task-gate] nudge, this is persisted: it
                    # is a real instruction that changed the model's behaviour, so
                    # replaying the transcript without it would break its meaning
                    # (docs/r1a-4-analysis.md D1).
                    books.note_strategy_nudge()
                    session.add_message(user_message(decision.inject or "", now_utc()))
                    self.closer.checkpoint(session, index)
                    continue

                books.note_iteration(index)
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
                )
                previous_request = assembled.previous_request

                # The one wait that cannot degrade: a model step either answers or
                # the turn ends (docs/r2-analysis.md §7.3).
                step = await self._model_step(
                    session=session,
                    request=assembled.request,
                    instructions=assembled.instructions,
                    deadline=frame.deadline,
                    on_delta=on_delta,
                    index=index,
                )
                books.note_step(step.text)

                if not step.tool_calls:
                    decision = self.policy.decide_after_step(
                        books.snapshot(
                            iteration=index,
                            tokens_spent=self.reporter.tokens_spent(),
                            open_items=task.open_items(),
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
                    books.note_nudge()
                    session.add_message(user_message(decision.inject or "", now_utc()))
                    self.closer.checkpoint(session, index)
                    continue

                outcomes = await self._run_tools(
                    step,
                    session=session,
                    task=task,
                    history=assembled.history,
                    instructions=assembled.instructions,
                    deadline=frame.deadline,
                    index=index,
                )
                books.note_outcomes(outcomes)
                if any(item.halted for item in outcomes):
                    outcome = stopped_outcome(step.text, index)
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
                outcome=aborted_outcome(books.last_text, books.iterations),
            )
            raise
        except (DeadlineExpired, EngineError) as exc:
            outcome = failed_outcome(
                exc, last_text=books.last_text, iterations=books.iterations
            )

        return await self._finish_turn(
            session,
            user_input=user_input,
            task=task,
            frame=frame,
            books=books,
            outcome=outcome,
        )

    async def _begin_turn(
        self,
        session: SessionData,
        user_input: str,
        observer: LoopObserver | None,
    ) -> TurnFrame:
        """Everything that must happen before the first iteration.

        Binding the UI, counting the turn, clearing per-turn policy state and
        asking ``turn.start`` hooks — none of it is a decision, so it does not
        belong in the loop body (docs/r7-analysis.md 3.1).
        """
        self._bind_observer(observer)
        self._bind_trace(session)
        session.add_message(user_message(user_input, now_utc()))
        # R6a: the session-level turn counter.  It is what `source_turn` and the
        # <working-memory> header mean by "turn N" (docs/r6-analysis.md 2.4).
        session.turn_count = int(getattr(session, "turn_count", 0) or 0) + 1
        self.closer.begin_turn()
        if session.task is None:
            session.task = TaskState(goal=user_input)
        task = session.task
        # R4b: guard state (duplicate calls, …) is per turn, never per process.
        self.dispatcher.guards.reset()
        self.reporter.begin_turn(self.observer)
        self._warn_tools_chars_once()
        hook_context = await self.turn_hooks.context(session)
        turn_timeout = float(getattr(self.settings, "turn_timeout_seconds", 0.0) or 0.0)
        return TurnFrame(
            turn_number=session.turn_count,
            task=task,
            hook_context=hook_context,
            deadline=Deadline.after(turn_timeout) if turn_timeout > 0 else None,
        )

    def _warn_tools_chars_once(self) -> None:
        """Say once, per loop, that the tool surface is over its ceiling (R7e-3)."""
        if self._tools_warned:
            return
        threshold = int(getattr(self.settings, "tools_chars_warn", 0) or 0)
        if threshold <= 0 or self._tools_chars <= threshold:
            return
        self._tools_warned = True
        biggest = next(iter(self._tools_by_origin), "")
        self._emit(
            ToolSurfaceWarning(
                tools_chars=self._tools_chars,
                threshold=threshold,
                origins=tuple(self._tools_by_origin.items()),
                hint=(
                    "工具 schema 每一步都会重发；"
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
    ) -> str:
        """Close the books: persist, harvest, fire ``turn.end``, hand back the text."""
        outcome = self._close_turn(
            session, user_input=user_input, task=task, frame=frame, books=books, outcome=outcome
        )
        await self.turn_memory.harvest(
            session,
            deadline=frame.deadline,
            summaries=books.summaries,
            outcome=outcome,
            # R6d: the turn's provenance for whatever it wrote — a fact the loop
            # already has, so memory can record where a note came from without
            # asking the model (and without filtering on it: D-R6-10).
            turn=frame.turn_number,
            evidence="tool" if books.successful_calls else "model",
        )
        await self.turn_hooks.fire(HookPoint.TURN_END, session)
        # R6a: a turn that ran out of budget used to hand the user an empty string.
        return outcome.text or outcome.handoff

    def _close_turn(
        self,
        session: SessionData,
        *,
        user_input: str,
        task: TaskState,
        frame: TurnFrame,
        books: TurnBooks,
        outcome: TurnOutcome,
    ) -> TurnOutcome:
        """Record what this turn leaves behind, then persist it (R6a)."""
        return self.closer.close(
            session,
            user_input=user_input,
            task=task,
            turn_number=frame.turn_number,
            outcome=outcome,
            failures=books.failures,
            trace_path=self._trace_path(session),
            observer=self.observer,
        )

    def _bind_observer(self, observer: LoopObserver | None) -> None:
        """Honour a per-call observer (the REPL swaps the UI between turns)."""
        if observer is not None:
            self.observer = SafeObserver(observer)
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
    ) -> list[DispatchOutcome]:
        """Run this step's tool calls and hand back what they did.

        R6b needs to see the outcomes (was anything attempted, did anything work,
        what failed last), so the caller — not this method — keeps the counters.
        Dispatch, write-back, the todo event and the mid-turn checkpoint all happen
        here, so the loop body reads as the ReAct cycle it is.
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
        self.closer.write_back(session, outcomes)
        if any(item.call.name == "todo_write" for item in outcomes):
            self._emit(TaskUpdated(task=task))
        self.closer.checkpoint(session, index)
        return outcomes

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
            self.closer.save(session)
        return report

    # ---- request assembly -------------------------------------------------

    @staticmethod
    def _remaining(deadline: Deadline | None) -> float | None:
        """Seconds left in the turn, or ``None`` when there is no ceiling."""
        return remaining_of(deadline)

    async def _build_instructions(self, deadline: Deadline | None = None) -> str | None:
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


__all__ = ["AgentLoop", "DeltaSink"]
