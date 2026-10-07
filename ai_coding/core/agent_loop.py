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
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
from ai_coding.core.acceptance import verify_items
from ai_coding.core.compaction import Compactor
from ai_coding.core.dispatch import DispatchOutcome, ToolDispatcher
from ai_coding.core.events import (
    AssistantMessage,
    InputSteered,
    IterationStarted,
    LedgerUpdated,
    LoopEvent,
    LoopObserver,
    NullObserver,
    ProviderFallback,
    ProviderRetried,
    TaskUpdated,
    ToolSurfaceWarning,
)
from ai_coding.core.guard import GuardChain, TurnView
from ai_coding.core.hooks import HookPoint
from ai_coding.core.memory_turn import TurnMemory
from ai_coding.core.observers import Braid, SafeObserver
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
from ai_coding.domain.acceptance import acceptance_states, pending_items
from ai_coding.domain.ledger import ledger_signature
from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolArtifact
from ai_coding.domain.model_config import ModelConfig
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
        models: Mapping[str, ModelConfig] | None = None,
        default_model: str = "",
        observer: LoopObserver | None = None,
        workspace: str = ".",
        trace: Any | None = None,
        guards: GuardChain | None = None,
        hooks: Any | None = None,
        steering: Any | None = None,
        cancel: asyncio.Event | None = None,
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
        # The model table lives on ``AppConfig``, not on ``AIConfig``: it is a
        # catalogue of connections, not a behaviour knob, and ``AIConfig`` is
        # shared by every entry point.  Without it ``/model`` could only ever
        # report "unknown model" (``set_model`` below is the only reader).
        self.models: dict[str, ModelConfig] = dict(models or {})
        # Remembered separately from ``engine.model_config`` so ``/model`` can
        # still mark the configured default after the user switched away.
        self.default_model = default_model
        self.workspace = workspace
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
        # R7e-2: where lines typed mid-turn wait.  None = today's behaviour, and the
        # loop then never looks at a queue at all.
        self.steering = steering
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
            cancel=cancel,
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

    # ---- the turn ---------------------------------------------------------

    async def process_input(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
    ) -> str:
        """Run one user turn to completion and return the final assistant text.

        Serialised per loop (G1-1).  A second caller waits rather than failing,
        which is the semantics the TUI already had when it queued submitted
        lines; rejecting instead would leave the front end queueing something the
        loop then refuses.
        """
        async with self._turn_lock:
            return await self._run_turn(session, user_input, on_delta, observer)

    async def _run_turn(
        self,
        session: SessionData,
        user_input: str,
        on_delta: DeltaSink | None = None,
        observer: LoopObserver | None = None,
    ) -> str:
        """One turn, ungated.  The body below is ``process_input`` unchanged:
        splitting it out keeps the re-entry guard a two-line diff instead of a
        re-indent of the whole turn."""
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
                # R0-2: derive the open unknowns from the books before deciding.
                # One call site per round (here, not after every branch), so a
                # flat round counts exactly once.
                self._refresh_ledger(books)
                decision = self.policy.decide_next_iteration(
                    books.snapshot(
                        iteration=index, tokens_spent=self.reporter.tokens_spent(),
                        # C3 reads the open set to decide whether the budget
                        # died with work unfinished.  The old ladder never
                        # needed it; without it the continuation never fires.
                        open_items=task.open_items(),
                        continuations=session.continuations,
                    ),
                    budget_expired=budget_expired,
                )
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
                    break
                if decision.action is TurnAction.NUDGE:
                    # R6b tier 2.  Like the [task-gate] nudge, this is persisted: it
                    # is a real instruction that changed the model's behaviour, so
                    # replaying the transcript without it would break its meaning
                    # (docs/r1a-4-analysis.md D1).
                    restart = False
                    if decision.reason == "uncertainty ledger flat":
                        books.note_ledger_nudge()
                    elif decision.reason == "continue unfinished work":
                        # C3: count the continuation on the session (it survives
                        # across rounds) and re-arm the iteration budget below —
                        # without the reset the counter keeps climbing, the new
                        # round sees "budget exhausted" at once, and the model is
                        # never called again (the cap burns for zero work).
                        session.continuations += 1
                        restart = True
                    else:
                        books.note_strategy_nudge()
                    session.add_message(user_message(decision.inject or "", now_utc()))
                    self.closer.checkpoint(session, index)
                    if restart:
                        index = 0
                    continue

                books.note_iteration(index)
                self._take_steering(session, books, index)
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
                    books=books,
                )

                if not step.tool_calls:
                    # R0-1: a turn with contracts does not end on the model's
                    # word — the proof commands run first, and the policy sees
                    # only their verdicts (its purity is what keeps the stop
                    # decision table-testable).
                    await self._verify_acceptance(session, books)
                    decision = self.policy.decide_after_step(
                        books.snapshot(
                            iteration=index,
                            tokens_spent=self.reporter.tokens_spent(),
                            open_items=task.open_items(),
                            final_text=step.text,
                            acceptance=acceptance_states(session.acceptance),
                            continuations=session.continuations,
                        )
                    )
                    if decision.action is TurnAction.FINISH:
                        outcome = TurnOutcome(
                            status=TurnStatus.COMPLETED,
                            text=step.text,
                            iterations=books.total_iterations,
                            reason=decision.reason,
                        )
                        break
                    if decision.reason == "acceptance checks failing":
                        books.note_acceptance_nudge()
                    else:
                        books.note_nudge()
                    session.add_message(user_message(decision.inject or "", now_utc()))
                    self.closer.checkpoint(session, index)
                    continue

                outcomes = await self._run_tools(
                    step,
                    session=session,
                    task=task,
                    books=books,
                    history=assembled.history,
                    instructions=assembled.instructions,
                    deadline=frame.deadline,
                    index=index,
                    turn=frame.turn_number,
                )
                books.note_outcomes(outcomes)
                if any(item.halted for item in outcomes):
                    outcome = stopped_outcome(step.text, books.total_iterations)
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
        except (DeadlineExpired, EngineError) as exc:
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
        # C3: the continuation allowance is per user turn — a new message
        # starts with a fresh budget, not the previous turn's leftovers.
        session.continuations = 0
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

    def _take_steering(self, session: SessionData, books: TurnBooks, index: int) -> None:
        """Absorb whatever the user typed, right before this request is assembled.

        Delivery point: the iteration boundary, after the previous batch finished -
        which is what Claude Code documents ("as soon as those tool calls finish,
        within the same turn", docs/r7e-analysis.md 2.1b).  The lines are persisted
        (Q4), and they consume neither an iteration nor a nudge.
        """
        if self.steering is None:
            return
        lines = self.steering.drain()
        if not lines:
            return
        for line in lines:
            session.add_message(user_message(f"[steering] {line}", now_utc()))
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
            # R8a D-14: what counts is evidence about the world, and a checkpoint
            # write is the turn talking about itself.
            evidence="tool" if books.evidence_calls else "model",
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
        # W1: every exit funnels through here (normal, failed, and the
        # cancelled path above), so the length-cutoff flag lands uniformly —
        # including on turns the budget or the clock stopped.
        outcome = replace(
            outcome, truncated_by_length=(books.last_finish_reason == "length")
        )
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
        if self.trace is not None:
            self.observer = Braid(SafeObserver(base), SafeObserver(self.trace))
        else:
            self.observer = SafeObserver(base)
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
                # R8a: the only channel a tool has to leave something in the
                # turn's book (checkpoint_write uses it).
                note_checkpoint=lambda text: books.note_checkpoint(text, limit=limit),
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

    def _refresh_ledger(self, books: TurnBooks) -> None:
        """Derive the open unknowns and emit when the set moves (R0-2).

        Emission is edge-triggered on the signature: a question appears once
        in the transcript, not as a per-round heartbeat.
        """
        items = books.ledger_items(
            stall_threshold=self.policy.config.stall_iterations
        )
        if books.note_ledger(ledger_signature(items), tuple(items)) and items:
            self._emit(LedgerUpdated(items=tuple(items)))

    async def _verify_acceptance(self, session: SessionData, books: TurnBooks) -> None:
        """Run the proof commands for unchecked acceptance items (R0-1).

        Only items that never passed are checked, so a green suite is not
        re-run every iteration.  Verdicts land on the session items (persisted
        by the normal save) and in the books (for the policy snapshot and the
        uncertainty ledger) — the policy itself never touches a shell.
        """
        pending = pending_items(session.acceptance)
        if not pending:
            return
        each = float(getattr(self.settings, "acceptance_verify_each_seconds", 120.0) or 0.0)
        total = float(getattr(self.settings, "acceptance_verify_total_seconds", 300.0) or 0.0)
        checked = await verify_items(
            pending, self.workspace, timeout_each=each or 120.0, timeout_total=total or 300.0
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
        try:
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
        self.reporter.report_step(
            index=index, step=step, latency_ms=latency_ms
        )
        self._emit(AssistantMessage(
            text=step.text, tool_calls=tuple(step.tool_calls),
            reasoning=step.reasoning))
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
