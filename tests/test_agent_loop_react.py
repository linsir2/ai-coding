"""TDD (R1a-4): AgentLoop — the self-owned ReAct loop.

Replaces the three M1–M3 loop test files.  Their ``FakeAIService`` implemented the
``AIService`` port that R1a-2 removed, and they asserted the old model where the
*engine* executed tools and returned their outputs.  The loop now owns tool
execution itself, so the spec genuinely changed — these tests script the engine
one step at a time and assert the loop's own choreography.
"""

import asyncio

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

    def __init__(self, name: str, *, result: ToolResult | None = None) -> None:
        self._name = name
        self._result = result or ToolResult(success=True, output=f"{name} ok")
        self.calls: list[dict] = []
        self.contexts: list = []

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
        registry.register(_Tool("read"))
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
    assert sessions.saves == 3


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

    # long session: no per-iteration checkpoint at index 1, only the final save
    assert sessions.saves == 1


# ---- stop conditions -------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iterations_enforced(tmp_path):
    engine = ScriptedEngine(
        [_calls(_ref("c1", "read")), _calls(_ref("c2", "read")), _text("never reached")]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, settings=_settings(max_tool_iterations=2))

    await loop.process_input(sessions.create(), "go")

    assert len(engine.requests) == 2
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
    engine = ScriptedEngine([_text("a"), _text("b"), _text("c")])
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

    assert len(engine.requests) == 3  # two nudges, then the cap is reached
    nudges = [m for m in session.messages if "[task-gate]" in (m.content or "")]
    assert len(nudges) == 2
    assert nudges[0].role == "user"


@pytest.mark.asyncio
async def test_todo_aware_gives_up_after_max_nudges(tmp_path):
    engine = ScriptedEngine([_text("a"), _text("b")])
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

    assert len(engine.requests) == 2
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_strict_policy_ignores_the_nudge_cap(tmp_path):
    """`strict` means "keep going": the iteration budget is the only limit."""
    engine = ScriptedEngine([_text("a"), _text("b"), _text("c"), _text("d")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        settings=_settings(
            stop_policy="strict", max_todo_nudges=1, max_tool_iterations=4
        ),
    )
    session = sessions.create()
    session.task = TaskState(goal="g")
    session.task.add("still open")

    await loop.process_input(session, "go")

    # todo_aware would have stopped at 2; strict keeps nudging until the budget ends
    assert len(engine.requests) == 4
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

    assert text == ""
    assert loop.last_outcome is not None
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
