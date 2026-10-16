"""TDD (R1a-4): AgentLoop — the self-owned ReAct loop.

Replaces the three M1–M3 loop test files.  Their ``FakeAIService`` implemented the
``AIService`` port that R1a-2 removed, and they asserted the old model where the
*engine* executed tools and returned their outputs.  The loop now owns tool
execution itself, so the spec genuinely changed — these tests script the engine
one step at a time and assert the loop's own choreography.
"""

import asyncio
import time

import pytest

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import (
    RecordingObserver,
    TaskUpdated,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from ai_coding.core.turn_policy import TASK_MESSAGE_LEAD
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import ApprovalResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.todo_write_tool import TodoWriteTool


def _ref(call_id: str, name: str, arguments: str = "{}") -> ToolCallRef:
    return ToolCallRef(id=call_id, name=name, arguments=arguments)


def _text(text: str) -> StepResult:
    return StepResult(text=text, finish_reason="stop")


def _calls(*refs: ToolCallRef, text: str | None = None) -> StepResult:
    return StepResult(text=text, tool_calls=list(refs), finish_reason="tool_calls")


class _Tool(BaseTool):
    """Minimal registry entry; records the params it was called with."""

    def __init__(
        self,
        name: str,
        *,
        result: ToolResult | None = None,
        mutates_world: bool = True,
    ) -> None:
        self._name = name
        self._result = result or ToolResult(success=True, output=f"{name} ok")
        self._mutates_world = mutates_world
        self.calls: list[dict] = []
        self.contexts: list = []

    @property
    def mutates_world(self) -> bool:
        return self._mutates_world

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.calls.append(params)
        self.contexts.append(ctx)
        return self._result


class ScriptedEngine(ChatEngine):
    """Returns pre-scripted steps and records every request it received."""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.requests: list[dict] = []
        self.closed = False

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(
            {
                "messages": list(messages),
                "tools": list(tools),
                "instructions": instructions,
            }
        )
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        self.closed = True


class _CountingSessions(SessionService):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.saves = 0

    def save(self, session: SessionData) -> None:
        self.saves += 1
        super().save(session)

    def mark_pending_approval(self, session_id: str, request: object) -> None:
        """P3: stand-in for the marker the real SessionService writes beside
        the session file; this fake keeps nothing on disk."""
        return None


class _Yes:
    async def approve(self, tool_name, params, reason):
        return ApprovalResult.YES


class _Stop:
    async def approve(self, tool_name, params, reason):
        return ApprovalResult.STOP


class _CancellingEngine(ChatEngine):
    """Raises CancelledError from the very first step."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        raise asyncio.CancelledError()


class _BrokenObserver:
    """A UI observer that explodes on every event."""

    def on_event(self, event) -> None:
        raise RuntimeError("ui exploded")


def _settings(**over) -> AIConfig:
    base = {"stop_policy": "natural"}
    base.update(over)
    return AIConfig(**base)


def _registry(*extra: BaseTool) -> ToolRegistry:
    """Default stack (read + todo_write) with ``extra`` overriding by name."""
    registry = ToolRegistry()
    provided = {tool.name for tool in extra}
    if "read" not in provided:
        # The real ReadTool is mutates_world=False (read_tool.py:64); the
        # double follows the interface (sanctioned extension).
        registry.register(_Tool("read", mutates_world=False))
    if "todo_write" not in provided:
        registry.register(TodoWriteTool())
    for tool in extra:
        registry.register(tool)
    return registry


def _loop(engine, sessions, registry=None, **kwargs) -> AgentLoop:
    registry = registry or _registry()
    kwargs.setdefault("settings", _settings())
    return AgentLoop(engine, sessions, registry, **kwargs)


def _last_request(engine) -> dict:
    return engine.requests[-1]


def _request_messages(engine, index: int = 0) -> list:
    return engine.requests[index]["messages"]


# ---- basics ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_step_no_tools_persists_user_and_assistant(tmp_path):
    engine = ScriptedEngine([_text("hello")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    text = await loop.process_input(session, "hi")

    assert text == "hello"
    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.messages[0].content == "hi"
    assert session.messages[1].content == "hello"
    persisted = sessions.load(session.session_id)
    assert persisted is not None
    assert [m.role for m in persisted.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_returns_final_text(tmp_path):
    engine = ScriptedEngine([_text("final")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    assert await loop.process_input(sessions.create(), "go") == "final"


@pytest.mark.asyncio
async def test_delta_callback_forwarded(tmp_path):
    engine = ScriptedEngine([_text("streamed")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    deltas: list[str] = []
    await loop.process_input(sessions.create(), "go", on_delta=deltas.append)
    assert deltas == ["streamed"]


@pytest.mark.asyncio
async def test_engine_receives_history_including_user_once(tmp_path):
    engine = ScriptedEngine([_text("ok")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    await loop.process_input(sessions.create(), "ask-me")
    sent = _request_messages(engine)
    assert sum(1 for m in sent if m.content == "ask-me") == 1


# ---- multi-iteration tool loop -------------------------------------------


@pytest.mark.asyncio
async def test_three_iterations_persist_all_assistant_and_tool_messages(tmp_path):
    engine = ScriptedEngine(
        [
            _calls(_ref("c1", "read")),
            _calls(_ref("c2", "read")),
            _text("done"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")

    assert [m.role for m in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert len(engine.requests) == 3


@pytest.mark.asyncio
async def test_tool_messages_pair_with_call_ids(tmp_path):
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")

    declared = {
        call.id
        for m in session.messages
        if m.role == "assistant" and m.tool_calls
        for call in m.tool_calls
    }
    produced = {m.tool_call_id for m in session.messages if m.role == "tool"}
    assert declared == {"c1"}
    assert produced == {"c1"}


@pytest.mark.asyncio
async def test_loop_stops_when_model_returns_no_tool_calls(tmp_path):
    engine = ScriptedEngine([_text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    await loop.process_input(sessions.create(), "go")
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_tool_result_is_fed_back_to_next_step(tmp_path):
    tool = _Tool("read", result=ToolResult(success=True, output="file body"))
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, _registry(tool))

    await loop.process_input(sessions.create(), "go")

    second = _request_messages(engine, 1)
    assert any(m.role == "tool" and "file body" in (m.content or "") for m in second)
    assert any("<tool_output" in (m.content or "") for m in second if m.role == "tool")


@pytest.mark.asyncio
async def test_multiple_calls_in_one_message_all_executed(tmp_path):
    reader = _Tool("read")
    engine = ScriptedEngine([_calls(_ref("c1", "read"), _ref("c2", "read")), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, _registry(reader))
    session = sessions.create()

    await loop.process_input(session, "go")

    assert len(reader.calls) == 2
    assert {m.tool_call_id for m in session.messages if m.role == "tool"} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_loop_persists_after_each_iteration(tmp_path):
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _calls(_ref("c2", "read")), _text("x")])
    sessions = _CountingSessions(tmp_path / "sessions")
    loop = _loop(engine, sessions)

    await loop.process_input(sessions.create(), "go")

    # two checkpoints (short session: one per iteration) plus the final save
    # (11-plan T5: plus the entry save that makes the user message durable)
    assert sessions.saves == 4


@pytest.mark.asyncio
async def test_checkpoint_throttling_on_long_sessions(tmp_path):
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("x")])
    sessions = _CountingSessions(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(persist_checkpoint_messages=10))
    session = sessions.create()
    for i in range(12):
        session.add_message(
            ChatMessage(
                role="user" if i % 2 == 0 else "assistant",
                content=f"pad {i}",
                timestamp="t",
            )
        )

    await loop.process_input(session, "go")

    # long session: no per-iteration checkpoint at index 1, only the final
    # save (11-plan T5: plus the entry save that makes the user message durable)
    assert sessions.saves == 2


# ---- stop conditions -------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iterations_enforced(tmp_path):
    engine = ScriptedEngine(
        [_calls(_ref("c1", "read")), _calls(_ref("c2", "read")), _text("never reached")]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(max_tool_iterations=2))

    await loop.process_input(sessions.create(), "go")

    # Two iterations, then the one closing call G8-2 asks for: the budget is
    # what stopped this turn, so the model gets a chance to say what it did
    # rather than leaving the harness to write a status report.
    assert len(engine.requests) == 3
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_max_iterations_keeps_partial_transcript(tmp_path):
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _calls(_ref("c2", "read")), _text("x")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(max_tool_iterations=2))
    session = sessions.create()

    await loop.process_input(session, "go")

    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    persisted = sessions.load(session.session_id)
    assert persisted is not None and len(persisted.messages) == 5


@pytest.mark.asyncio
async def test_stop_policy_natural_ignores_open_todos(tmp_path):
    engine = ScriptedEngine([_text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(stop_policy="natural"))
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "go")

    assert len(engine.requests) == 1
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_todo_aware_nudges_when_open_items_remain(tmp_path):
    engine = ScriptedEngine([_text("a"), _text("b"), _text("c"), _text("d")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        settings=_settings(stop_policy="todo_aware", max_todo_nudges=2),
    )
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "go")

    # 13 §2.1: two L1 nudges, then the ask level (L2); an ignored ask is the
    # ladder's stop (the 4th model step sees it).
    assert len(engine.requests) == 4
    nudges = [m for m in session.messages if "[task-gate]" in (m.content or "")]
    assert len(nudges) == 3
    assert nudges[0].role == "user"
    assert "ask_user" in (nudges[-1].content or "")   # L2 asks the user
    assert loop.last_outcome is not None
    assert loop.last_outcome.reason == "todo ladder exhausted"


@pytest.mark.asyncio
async def test_todo_aware_gives_up_after_max_nudges(tmp_path):
    engine = ScriptedEngine([_text("a"), _text("b"), _text("c")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        settings=_settings(stop_policy="todo_aware", max_todo_nudges=1),
    )
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "go")

    # 13 §2.1: L1 ×1 → L2 ask ×1 → an ignored ask ends the ladder.
    assert len(engine.requests) == 3
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED
    assert loop.last_outcome.reason == "todo ladder exhausted"


@pytest.mark.asyncio
async def test_strict_policy_ignores_the_nudge_cap(tmp_path):
    """`strict` means "keep going": the iteration budget is the only limit."""
    engine = ScriptedEngine([_text("a"), _text("b"), _text("c"), _text("d")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        settings=_settings(
            stop_policy="strict", max_todo_nudges=1, max_tool_iterations=4,
            # C3 off: this test is about the nudge cap inside one iteration
            # round.  With C3 on, budget exhaustion with open items starts a
            # fresh round (by design), which is a different mechanism.
            max_auto_continuations=0,
        ),
    )
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "go")

    # todo_aware would have stopped at 2; strict keeps nudging until the budget
    # ends, and then the closing call (G8-2) is one more.
    assert len(engine.requests) == 5
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_todo_aware_does_not_nudge_when_all_terminal(tmp_path):
    engine = ScriptedEngine([_text("a")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(stop_policy="todo_aware"))
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("finished")
    session.task.update("1", status="completed")

    await loop.process_input(session, "go")

    assert len(engine.requests) == 1
    assert not [m for m in session.messages if "[task-gate]" in (m.content or "")]


@pytest.mark.asyncio
async def test_approval_stop_sets_stopped_status(tmp_path):
    writer = _Tool("write")
    engine = ScriptedEngine([_calls(_ref("c1", "write", '{"path":"a"}'))])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, _registry(writer), approval=_Stop())
    session = sessions.create()

    await loop.process_input(session, "go")

    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.STOPPED
    assert len(engine.requests) == 1  # no further model call after STOP
    tools = [m for m in session.messages if m.role == "tool"]
    assert len(tools) == 1
    assert "stopped" in (tools[0].content or "")


@pytest.mark.asyncio
async def test_turn_outcome_records_iterations_and_reason(tmp_path):
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _calls(_ref("c2", "read"))])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(max_tool_iterations=2))

    await loop.process_input(sessions.create(), "go")

    outcome = loop.last_outcome
    assert outcome is not None
    assert outcome.iterations == 2
    assert outcome.reason == "iteration budget exhausted"


# ---- task injection --------------------------------------------------------


@pytest.mark.asyncio
async def test_task_injection_message_is_not_persisted(tmp_path):
    engine = ScriptedEngine([_text("ok")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")

    assert any("<task-state>" in (m.content or "") for m in _request_messages(engine))
    assert not [m for m in session.messages if "<task-state>" in (m.content or "")]
    persisted = sessions.load(session.session_id)
    assert persisted is not None
    assert not [m for m in persisted.messages if "<task-state>" in (m.content or "")]


@pytest.mark.asyncio
async def test_task_injection_message_appears_last(tmp_path):
    engine = ScriptedEngine([_text("ok")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    await loop.process_input(sessions.create(), "go")
    last = _request_messages(engine)[-1]
    assert "<task-state>" in (last.content or "")
    assert "系统注入" in (last.content or "")


@pytest.mark.asyncio
async def test_task_injection_system_mode_puts_render_in_instructions(tmp_path):
    engine = ScriptedEngine([_text("ok")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(task_injection="system"))

    await loop.process_input(sessions.create(), "go")

    assert "<task-state>" in engine.requests[0]["instructions"]
    assert not [m for m in _request_messages(engine) if "<task-state>" in (m.content or "")]


@pytest.mark.asyncio
async def test_todo_write_updates_session_task(tmp_path):
    engine = ScriptedEngine(
        [
            _calls(_ref("c1", "todo_write", '{"action":"add","content":"ship it"}')),
            _text("added"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")

    assert session.task is not None
    assert [i.content for i in session.task.items] == ["ship it"]
    persisted = sessions.load(session.session_id)
    assert persisted is not None and persisted.task is not None
    assert [i.content for i in persisted.task.items] == ["ship it"]


@pytest.mark.asyncio
async def test_task_updated_event_emitted(tmp_path):
    engine = ScriptedEngine(
        [
            _calls(_ref("c1", "todo_write", '{"action":"add","content":"one"}')),
            _text("done"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = _loop(engine, sessions, observer=observer)

    await loop.process_input(sessions.create(), "go")

    updated = observer.of_type(TaskUpdated)
    assert len(updated) == 1
    assert [i.content for i in updated[0].task.items] == ["one"]


@pytest.mark.asyncio
async def test_tool_context_carries_request_view(tmp_path):
    reader = _Tool("read")
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, _registry(reader))
    session = sessions.create()

    await loop.process_input(session, "go")

    ctx = reader.contexts[0]
    assert ctx is not None
    assert ctx.session_id == session.session_id
    assert ctx.task is session.task
    assert ctx.messages  # the request view, not empty


@pytest.mark.asyncio
async def test_tool_context_emit_reaches_the_observer(tmp_path):
    """`ToolContext.emit` is the documented tool -> observer channel.

    R1a-3 deferred wiring it to R1a-4, and R1a-4 did not do it, so it silently
    discarded whatever a tool emitted.  This pins the contract.
    """

    class _EmittingTool(_Tool):
        async def execute(self, params, ctx=None):  # type: ignore[override]
            assert ctx is not None
            ctx.emit(ToolStarted(call_id="inner", tool_name="inner", params={}))
            return await super().execute(params, ctx)

    tool = _EmittingTool("read")
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = _loop(engine, sessions, _registry(tool), observer=observer)

    await loop.process_input(sessions.create(), "go")

    assert [event.tool_name for event in observer.of_type(ToolStarted)] == [
        "read",
        "inner",
    ]


# ---- robustness and events -------------------------------------------------


@pytest.mark.asyncio
async def test_engine_error_yields_error_status_and_persists(tmp_path):
    engine = ScriptedEngine([EngineError("boom")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    text = await loop.process_input(session, "go")

    # R6a changed exactly one outward behaviour: an unfinished turn hands back a
    # harness report instead of "".  `text` keeps its own meaning — the model never
    # produced a final message (docs/r6-analysis.md 2.1 / section 5).
    assert text.startswith("[回合未完成 · 引擎错误")
    assert loop.last_outcome is not None
    assert loop.last_outcome.text is None
    assert loop.last_outcome.handoff == text
    assert loop.last_outcome.status is TurnStatus.ERROR
    assert "boom" in loop.last_outcome.reason
    persisted = sessions.load(session.session_id)
    assert persisted is not None and persisted.messages[0].content == "go"


@pytest.mark.asyncio
async def test_cancellation_yields_aborted_and_cleans_pairing(tmp_path):
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(_CancellingEngine(), sessions)
    session = sessions.create()

    with pytest.raises(asyncio.CancelledError):
        await loop.process_input(session, "go")

    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.ABORTED
    assert not [m for m in session.messages if m.role == "tool"]
    assert sessions.load(session.session_id) is not None


@pytest.mark.asyncio
async def test_turn_finished_event_emitted_last(tmp_path):
    engine = ScriptedEngine([_text("ok")])
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = _loop(engine, sessions, observer=observer)

    await loop.process_input(sessions.create(), "go")

    assert isinstance(observer.events[-1], TurnFinished)


@pytest.mark.asyncio
async def test_observer_exception_does_not_break_turn(tmp_path):
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, observer=_BrokenObserver())
    session = sessions.create()

    text = await loop.process_input(session, "go")

    assert text == "done"
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_tool_lifecycle_events_emitted(tmp_path):
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = _loop(engine, sessions, observer=observer)

    await loop.process_input(sessions.create(), "go")

    started = observer.of_type(ToolStarted)
    finished = observer.of_type(ToolFinished)
    assert [e.call_id for e in started] == ["c1"]
    assert [e.call_id for e in finished] == ["c1"]
    assert finished[0].success is True


@pytest.mark.asyncio
async def test_open_todos_persist_across_turns(tmp_path):
    engine = ScriptedEngine(
        [
            _calls(_ref("c1", "todo_write", '{"action":"add","content":"ship it"}')),
            _text("added"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    session = sessions.create()
    await _loop(engine, sessions).process_input(session, "first")

    engine2 = ScriptedEngine([_text("ok")])
    await _loop(engine2, sessions).process_input(session, "second")

    assert session.task is not None and session.task.total == 1
    second_request = _request_messages(engine2)[-1]
    assert "ship it" in (second_request.content or "")


# ---- R2a: the turn watchdog ------------------------------------------------
# Iteration counts cannot bound a call that never returns, so the loop needs a
# wall-clock ceiling: any stuck step or tool must still produce a terminal
# TurnOutcome (docs/r2-analysis.md section 2/R2a, section 7.3).


class _HangingTool(BaseTool):
    """Never returns, and is slow to acknowledge cancellation."""

    def __init__(self, name: str = "read", *, ceiling: float | None = 0.1) -> None:
        self._name = name
        self._ceiling = ceiling
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def max_duration_seconds(self) -> float | None:
        return self._ceiling

    @property
    def description(self) -> str:
        return "hangs forever"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.calls += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # like communicate() with a live grandchild
            raise
        return ToolResult(success=True, output="never")


class _HangingEngine(ChatEngine):
    """A model step that never returns (the deadline must abandon it)."""

    def __init__(self) -> None:
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise
        raise AssertionError("unreachable")


class _SlowEngine(ChatEngine):
    """Answers correctly, just not instantly."""

    def __init__(self, delay: float, text: str) -> None:
        self._delay = delay
        self._text = text

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        await asyncio.sleep(self._delay)
        return StepResult(text=self._text, finish_reason="stop")


class _HangsAfterFirstStep(ChatEngine):
    """First step asks for a tool that times out; the second never returns."""

    def __init__(self, ref: ToolCallRef) -> None:
        self._first = StepResult(text=None, tool_calls=[ref], finish_reason="tool_calls")
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return self._first
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_turn_deadline_ends_turn_as_timeout(tmp_path):
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = _loop(
        _HangingEngine(),
        sessions,
        observer=observer,
        settings=_settings(turn_timeout_seconds=0.2),
    )
    session = sessions.create()

    started = time.monotonic()
    text = await loop.process_input(session, "go")
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the watchdog let the turn run {elapsed:.1f}s"
    # R6a: same contract change as the engine-error case above — the turn reports
    # through `handoff`, and `text` still says "the model said nothing".
    assert text.startswith("[回合未完成 · 回合超时")
    assert loop.last_outcome is not None
    assert loop.last_outcome.text is None
    assert loop.last_outcome.handoff == text
    assert loop.last_outcome.status is TurnStatus.TIMEOUT
    assert "turn deadline" in loop.last_outcome.reason
    assert isinstance(observer.events[-1], TurnFinished)
    assert observer.events[-1].outcome.status is TurnStatus.TIMEOUT
    assert sessions.load(session.session_id) is not None


@pytest.mark.asyncio
async def test_turn_deadline_after_a_tool_timeout_leaves_a_valid_transcript(tmp_path):
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        _HangsAfterFirstStep(_ref("c1", "read")),
        sessions,
        registry=_registry(_HangingTool("read")),
        settings=_settings(turn_timeout_seconds=0.4),
    )
    session = sessions.create()

    await loop.process_input(session, "go")

    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.TIMEOUT
    roles = [message.role for message in session.messages]
    assert "tool" in roles  # the timed-out call still got a paired result
    session.ensure_tool_pairing()
    assert [message.role for message in session.messages] == roles


@pytest.mark.asyncio
async def test_tool_timeout_lets_the_model_recover(tmp_path):
    tool = _HangingTool("read")
    engine = ScriptedEngine([_calls(_ref("c1", "read")), _text("recovered")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        registry=_registry(tool),
        settings=_settings(turn_timeout_seconds=5),
    )
    session = sessions.create()

    started = time.monotonic()
    text = await loop.process_input(session, "go")

    assert text == "recovered"
    assert time.monotonic() - started < 3.0
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED
    assert loop.last_outcome.iterations == 2
    tool_messages = [message for message in session.messages if message.role == "tool"]
    assert len(tool_messages) == 1
    assert "budget" in (tool_messages[0].content or "")
    # The model saw the failure on the next step instead of waiting forever.
    # (The last message is the volatile task-state block, so look for the tool.)
    assert "tool" in [message.role for message in engine.requests[1]["messages"]]


@pytest.mark.asyncio
async def test_zero_turn_timeout_disables_the_watchdog(tmp_path):
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        _SlowEngine(0.15, "slow but fine"),
        sessions,
        settings=_settings(turn_timeout_seconds=0),
    )

    assert await loop.process_input(sessions.create(), "go") == "slow but fine"
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED


# ---- R8c: Esc cancels one batch, and the turn carries on --------------------


class _HangsOnFirstCall(BaseTool):
    """Hangs on its first call; the second one proves the cancel was cleared."""

    parallel_safe = True

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "hangs once"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(30)
        return ToolResult(success=True, output="the second call ran")


class _AsksTwiceThenAnswers(ChatEngine):
    def __init__(self) -> None:
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if self.calls <= 2:
            return _calls(_ref(f"c{self.calls}", "read"))
        return _text("好了")


@pytest.mark.asyncio
async def test_r8_a_cancel_does_not_end_the_turn_and_clears_before_the_next_batch(tmp_path):
    cancel = asyncio.Event()
    sessions = SessionService(tmp_path / "sessions")
    tool = _HangsOnFirstCall()
    loop = _loop(
        _AsksTwiceThenAnswers(),
        sessions,
        registry=_registry(tool),
        cancel=cancel,
        settings=_settings(max_tool_iterations=5),
    )
    session = sessions.create()

    async def trip() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    answer = await asyncio.gather(loop.process_input(session, "先跑一次"), trip())

    assert answer[0] == "好了"
    tool_messages = [m.content or "" for m in session.messages if m.role == "tool"]
    assert any("cancelled_by_user" in body for body in tool_messages)
    assert any("the second call ran" in body for body in tool_messages)
    assert tool.calls == 2  # the second batch really ran, not cancelled again
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED


# ---- G8-2: the model gets to write the closing, the banner still stands -----


@pytest.mark.asyncio
async def test_the_model_is_asked_to_close_when_the_iteration_budget_ends(tmp_path):
    """The harness report is a status ("steps exhausted"), not an answer.  One
    more call turns it into prose the operator can act on -- inside the report,
    so a cut-off turn still reads as cut off."""
    engine = ScriptedEngine(
        [
            _calls(_ref("c1", "read")),
            _calls(_ref("c2", "read")),
            _text("读了两次，还差一个结论。"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(max_tool_iterations=2))

    text = await loop.process_input(sessions.create(), "go")

    assert len(engine.requests) == 3, "one iteration past the budget, for the closing"
    assert engine.requests[-1]["messages"][-1].content.startswith("本轮步数预算已用完")
    assert "[回合未完成" in text
    assert "读了两次，还差一个结论。" in text


@pytest.mark.asyncio
async def test_a_turn_stopped_by_the_clock_is_not_asked_to_close(tmp_path):
    """Nothing is left to spend: the clock ran out, so an extra call would be
    refused by the deadline rather than answered.

    The engine has to actually hang -- a scripted engine returns instantly, the
    clock never fires, and the turn ends on the iteration budget instead, which
    is a different path.
    """
    engine = _HangingEngine()
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(turn_timeout_seconds=0.2))

    await loop.process_input(sessions.create(), "go")

    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.TIMEOUT
    assert engine.calls == 1, "the closing call must not be attempted after the clock"


@pytest.mark.asyncio
async def test_a_turn_with_no_room_left_in_its_token_budget_is_not_asked(tmp_path):
    """The token ceiling is precisely the budget that is already gone -- asking
    again would spend the one resource that is out."""
    costly = StepResult(
        tool_calls=[_ref("c1", "read")],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 5_000, "completion_tokens": 10},
    )
    engine = ScriptedEngine([costly])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        settings=_settings(max_tool_iterations=5, turn_token_budget=1_000),
    )

    await loop.process_input(sessions.create(), "go")

    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.BUDGET
    # One step spent the ceiling; the ladder stops on the token gate, and the
    # closing call never happens because that budget is what ran out.
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_a_failed_closing_call_does_not_fail_the_turn(tmp_path):
    """A courtesy must not be able to turn a finished turn into an error."""

    # An *instance*: the scripted engine raises what it is handed, and calling
    # a function here would raise during setup instead of during the turn.
    engine = ScriptedEngine(
        [
            _calls(_ref("c1", "read")),
            _calls(_ref("c2", "read")),
            RuntimeError("provider exploded"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(max_tool_iterations=2))

    text = await loop.process_input(sessions.create(), "go")

    assert "[回合未完成" in text
    assert "provider exploded" not in text


# ---- 13 §2.5 (B): a broken stream continues from where it stopped -----------


class _BrokenMidStream(ChatEngine):
    """First ``breaks`` calls: half an answer, then kill the stream."""

    def __init__(self, breaks: int = 1) -> None:
        self.breaks = breaks
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        if self.breaks > 0:
            self.breaks -= 1
            if on_delta is not None:
                on_delta("half an answer")
            # What openai_engine._stream raises once text has left the building.
            raise EngineError("stream failed after 1 text chunk(s): boom")
        return StepResult(text="done", finish_reason="stop")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_interrupted_stream_is_continued_once_with_a_persisted_note(tmp_path):
    engine = _BrokenMidStream()
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    text = await loop.process_input(session, "go")

    assert len(engine.requests) == 2, "first step broke; the continuation is the second"
    assert text == "done"
    assert any(
        "[stream-interrupted]" in (m.content or "") for m in session.messages
    ), "the synthetic note must be persisted (same discipline as the gate nudges)"
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_second_interruption_stays_a_failure(tmp_path):
    engine = _BrokenMidStream(breaks=2)
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")

    notes = [
        m for m in session.messages if "[stream-interrupted]" in (m.content or "")
    ]
    assert len(notes) == 1, "the continuation is bounded to once per turn"
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.ERROR


@pytest.mark.asyncio
async def test_an_interruption_with_no_output_is_not_continued(tmp_path):
    class _DeadPipe(ChatEngine):
        """A typed failure before any delta: the ordinary failure semantics."""

        def __init__(self) -> None:
            self.requests = 0

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self.requests += 1
            raise EngineError("transport died", kind="transport")

    engine = _DeadPipe()
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")

    assert engine.requests == 1
    assert not any(
        "[stream-interrupted]" in (m.content or "") for m in session.messages
    )
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.ERROR


# ---- 13 §2.7 (G/H): the closing step and the empty step ---------------------


@pytest.mark.asyncio
async def test_the_closing_step_offers_no_tools(tmp_path):
    """G (13 §2.7): a closing step has no next iteration to execute tools in,
    so the request must not offer any (probe P4: calls were silently dropped)."""
    engine = ScriptedEngine(
        [
            _calls(_ref("c1", "read")),
            _calls(_ref("c2", "read")),
            _text("收尾词。"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(max_tool_iterations=2))

    await loop.process_input(sessions.create(), "go")

    assert len(engine.requests) == 3
    assert engine.requests[-1]["tools"] == [], "the closing call offers no tools"
    assert engine.requests[0]["tools"], "ordinary steps still carry the catalog"


@pytest.mark.asyncio
async def test_a_single_empty_step_is_retried_once(tmp_path):
    """H (13 §2.7): a three-way-empty step earns one retry of the same request
    (dsh EMPTY_RESPONSE parity, conservative bound of 1)."""
    empty = StepResult(
        text=None, tool_calls=(), finish_reason="stop", usage=None, reasoning=None
    )
    engine = ScriptedEngine([empty, _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)

    text = await loop.process_input(sessions.create(), "go")

    assert text == "done"
    assert len(engine.requests) == 2
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED
    # No injected message: the retry is the same request (dsh llm-retry parity).
    assert engine.requests[0]["messages"] == engine.requests[1]["messages"]


@pytest.mark.asyncio
async def test_two_empty_steps_keep_the_failure_semantics(tmp_path):
    empty = StepResult(
        text=None, tool_calls=(), finish_reason="stop", usage=None, reasoning=None
    )
    engine = ScriptedEngine([empty, empty])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)

    await loop.process_input(sessions.create(), "go")

    assert len(engine.requests) == 2
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.ERROR
    assert "没有回答" in loop.last_outcome.reason


def _task_blocks(engine, index: int = -1) -> list[str]:
    return [
        message.content or ""
        for message in engine.requests[index]["messages"]
        if TASK_MESSAGE_LEAD in (message.content or "")
    ]


@pytest.mark.asyncio
async def test_a_bare_goal_tracks_the_current_turn_not_the_opening_message(tmp_path):
    """Measured (session d68080eb, 2026-10-15): the opening message "你好"
    froze ``TaskState.goal``, every later request's <task-state> tail block kept
    advertising it, and after a long tool chain the model anchored on the tail
    and answered the greeting again.  A bare goal (no items) must refresh to
    the turn's own instruction."""
    engine = ScriptedEngine([_text("你好呀"), _text("这是回答")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "你好")
    await loop.process_input(session, "你的编排层怎么设计的")

    first, last = _task_blocks(engine, 0), _task_blocks(engine)
    assert "目标: 你好" in first[0]
    assert len(last) == 1
    assert "目标: 你的编排层怎么设计的" in last[0]
    assert "目标: 你好" not in last[0]


@pytest.mark.asyncio
async def test_a_goal_with_items_survives_a_follow_up_message(tmp_path):
    """A real list names what its items are for: a follow-up question while a
    plan is on the books must not rewrite the goal."""
    engine = ScriptedEngine([_text("ok")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.task = TaskState(goal="让 make test 通过")
    session.task.add("修失败用例")

    await loop.process_input(session, "顺便问下，覆盖率怎么算的")

    blocks = _task_blocks(engine)
    assert "目标: 让 make test 通过" in blocks[0]
    assert "覆盖率" not in blocks[0]


@pytest.mark.asyncio
async def test_plan_mode_finishes_without_nudging_open_items(tmp_path):
    """18 §10.5 (R4): the execution gates are silent under a posture — the
    snapshot carries ``mode`` off the session, so this must hold end-to-end.
    stage 4 r2: the plan SUBMISSION guard is not an execution gate — no plan
    was ever submitted, so it spends its one remedy and then finishes with
    the guarded reason; the task gate still never speaks."""
    engine = ScriptedEngine([_text("done"), _text("done again")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(stop_policy="todo_aware"))
    session = sessions.create()
    session.mode = "plan"
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "go")

    assert len(engine.requests) == 2  # the remedy round
    assert not [m for m in session.messages if "[task-gate]" in (m.content or "")]
    reminders = [
        m
        for m in session.messages
        if m.synthetic and "present_plan" in (m.content or "")
    ]
    assert len(reminders) == 1
    assert loop.last_outcome is not None
    assert loop.last_outcome.reason == "plan unsubmitted"


@pytest.mark.asyncio
async def test_plan_mode_does_not_refresh_a_bare_goal(tmp_path):
    """18 §10.5 (R5): the goal topic belongs to the planning axis — a new turn
    in plan mode must not steal it with its own instruction.  stage 4 r2:
    each bare-text ending spends the guard's one remedy, so the script gets
    a second step per turn (the remedy round)."""
    engine = ScriptedEngine(
        [_text("你好呀"), _text("好的"), _text("这是回答"), _text("收到")]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.mode = "plan"

    await loop.process_input(session, "你好")
    await loop.process_input(session, "你的编排层怎么设计的")

    last = _task_blocks(engine)
    assert len(last) == 1
    assert "目标: 你好" in last[0]
    assert "你的编排层怎么设计的" not in last[0]


@pytest.mark.asyncio
async def test_readonly_hides_mutating_tools_from_the_request(tmp_path):
    """只读探索批次: the schema list mirrors the dispatch gate's own predicate
    (``mutates_world``) -- what the gate would refuse is never advertised, so
    the model has nothing to walk into.  Reading tools stay."""
    # Two steps: the readonly posture also runs the plan guard, which spends
    # its one remedy round before the guarded finish.
    engine = ScriptedEngine([_text("done"), _text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, registry=_registry(_Tool("bash")))
    session = sessions.create()
    session.readonly = True

    await loop.process_input(session, "go")

    names = {tool["function"]["name"] for tool in _last_request(engine)["tools"]}
    assert "bash" not in names
    assert {"read", "todo_write"} <= names


@pytest.mark.asyncio
async def test_a_writable_turn_keeps_the_full_tool_surface(tmp_path):
    engine = ScriptedEngine([_text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, registry=_registry(_Tool("bash")))
    session = sessions.create()

    await loop.process_input(session, "go")

    names = {tool["function"]["name"] for tool in _last_request(engine)["tools"]}
    assert "bash" in names
