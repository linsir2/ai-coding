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
from typing import TYPE_CHECKING

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
from ai_coding.core.dispatch import DispatchOutcome, ToolDispatcher
from ai_coding.core.events import (
    AssistantMessage,
    IterationStarted,
    LoopEvent,
    LoopObserver,
    NullObserver,
    TaskUpdated,
    TurnFinished,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolOutput
from ai_coding.domain.run import assistant_message, tool_message, user_message
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState, TodoItem
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

TASK_MESSAGE_LEAD = "以下是你自己的任务清单状态（系统注入，不是用户发言）："
TASK_GATE_LEAD = "[task-gate] 你还有未完成的任务项："
TASK_GATE_TAIL = "请继续推进，或把不再需要的条目用 todo_write 标记为 dropped 并说明原因。"
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
        self._tool_schemas = tool_schemas(tools)
        self.dispatcher = ToolDispatcher(
            tools,
            gate or PermissionGate(),
            approval,
            max_parallel=self.settings.max_concurrent_tools,
            observer=self.observer,
        )
        self.last_outcome: TurnOutcome | None = None

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
        if session.task is None:
            session.task = TaskState(goal=user_input)
        task = session.task

        settings = self.settings
        max_iterations = max(1, int(settings.max_tool_iterations))
        iterations = 0
        nudges = 0
        last_text: str | None = None
        outcome = TurnOutcome(
            status=TurnStatus.MAX_ITERATIONS, reason="iteration budget exhausted"
        )

        try:
            for index in range(1, max_iterations + 1):
                iterations = index
                self._emit(IterationStarted(index=index, max_iterations=max_iterations))

                history = session.messages
                if self.context is not None:
                    history = await self.context.compress(history)
                instructions = await self._build_instructions(history)
                request, instructions = self._inject_task(history, instructions, task)

                step = await self.engine.step(
                    request,
                    tools=self._tool_schemas,
                    instructions=instructions,
                    on_delta=on_delta,
                )
                last_text = step.text
                self._emit(
                    AssistantMessage(text=step.text, tool_calls=tuple(step.tool_calls))
                )
                session.add_message(assistant_message(step, now_utc()))

                if not step.tool_calls:
                    open_items = task.open_items()
                    if (
                        settings.stop_policy == "natural"
                        or not open_items
                        or nudges >= settings.max_todo_nudges
                    ):
                        outcome = TurnOutcome(
                            status=TurnStatus.COMPLETED,
                            text=step.text,
                            iterations=index,
                            reason="model finished",
                        )
                        break
                    nudges += 1
                    session.add_message(user_message(self._nudge_text(open_items), now_utc()))
                    self._checkpoint(session, index)
                    continue

                outcomes = await self.dispatcher.dispatch_batch(
                    step.tool_calls, self._tool_context(session, task, history, instructions)
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
            else:
                outcome = TurnOutcome(
                    status=TurnStatus.MAX_ITERATIONS,
                    text=last_text,
                    iterations=iterations,
                    reason="iteration budget exhausted",
                )
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
        except EngineError as exc:
            outcome = TurnOutcome(
                status=TurnStatus.ERROR,
                text=None,
                iterations=iterations,
                reason=str(exc),
            )

        self._finalize(session, outcome)
        await self._remember(session.messages, outcome.text)
        return outcome.text or ""

    async def aclose(self) -> None:
        """Release engine resources (the loop owns nothing else)."""
        await self.engine.aclose()

    # ---- request assembly -------------------------------------------------

    async def _build_instructions(self, history: list[ChatMessage]) -> str | None:
        if self.prompt is None:
            return None
        memory_catalog = ""
        if self.memory is not None:
            try:
                memory_catalog = "\n".join(await self.memory.recall(history))
            except Exception:  # noqa: BLE001 — memory is best-effort
                memory_catalog = ""
        project_text = self.project.load() if self.project is not None else ""
        skills_catalog = self.skills.catalog_text() if self.skills is not None else ""
        return self.prompt.build(
            project_instructions=project_text,
            skills_catalog=skills_catalog,
            memory_catalog=memory_catalog,
        )

    def _inject_task(
        self,
        messages: list[ChatMessage],
        instructions: str | None,
        task: TaskState,
    ) -> tuple[list[ChatMessage], str | None]:
        """Attach the task list to this request only (never to the session)."""
        render = task.render()
        if self.settings.task_injection == "system":
            merged = f"{instructions}\n\n{render}" if instructions else render
            return list(messages), merged
        block = ChatMessage(
            role="user", content=f"{TASK_MESSAGE_LEAD}\n{render}", timestamp=""
        )
        return [*messages, block], instructions

    @staticmethod
    def _nudge_text(open_items: list[TodoItem]) -> str:
        lines = [TASK_GATE_LEAD]
        lines.extend(f"- {item.id}. {item.content}" for item in open_items[:10])
        lines.append(TASK_GATE_TAIL)
        return "\n".join(lines)

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
        )

    # ---- persistence ------------------------------------------------------

    def _write_tool_messages(
        self, session: SessionData, outcomes: list[DispatchOutcome]
    ) -> None:
        for item in outcomes:
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

    async def _remember(self, messages: list[ChatMessage], conclusion: str | None) -> None:
        if self.memory is None:
            return
        try:
            await self.memory.remember(messages, conclusion)
        except Exception:  # noqa: BLE001 — memory is best-effort
            pass

    # ---- events -----------------------------------------------------------

    def _emit(self, event: LoopEvent) -> None:
        self.observer.on_event(event)


__all__ = ["AgentLoop", "DeltaSink"]
