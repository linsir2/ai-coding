"""C-5b-2/3/4: send_message, interrupt_agent and resume_agent.

The turn-level control plane over ``SubAgentRunner``: a running turn takes a
message at its next boundary, an idle one starts a new turn, an interrupt cuts
the turn but keeps the child, and resume reopens the same conversation -- in
memory or from the sidecar + session file after a close or a restart.

Every test here is scripted: no provider, no network, no worktree.
"""

import asyncio

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import BackgroundTaskDone
from ai_coding.core.steering import SteeringQueue
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.domain.subagent import BackgroundTask, ParentContext, SubAgentPoolFull
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.permissions import Effect, Rule
from ai_coding.service.session_service import SessionService
from ai_coding.tools.agents_tool import (
    InterruptAgentTool,
    ResumeAgentTool,
    SendMessageTool,
)
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry

# ---- scripted engines --------------------------------------------------------


def _text(value: str) -> StepResult:
    return StepResult(text=value, finish_reason="stop")


def _call(call_id: str, name: str) -> StepResult:
    return StepResult(
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments="{}")],
        finish_reason="tool_calls",
    )


def _hang(entered: asyncio.Event):
    """A step that never finishes until cancelled (the interrupt case)."""

    async def step() -> StepResult:
        entered.set()
        await asyncio.sleep(3600)
        return _text("never")

    return step


class _Scripted(ChatEngine):
    """One engine instance, its own script: a step is consumed per request."""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.requests: list[list[ChatMessage]] = []
        self.closed = False

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        if not self._steps:
            raise AssertionError("engine called past its script")
        item = self._steps.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return await item()
        return item

    async def aclose(self) -> None:
        self.closed = True


class _Engines:
    """An engine factory: each loop build takes the next script (or "ok")."""

    def __init__(self) -> None:
        self._scripts: list[list] = []
        self.created: list[_Scripted] = []

    def push(self, *steps) -> None:
        self._scripts.append(list(steps))

    def __call__(self) -> _Scripted:
        steps = self._scripts.pop(0) if self._scripts else [_text("ok")]
        engine = _Scripted(steps)
        self.created.append(engine)
        return engine


class _PingTool(BaseTool):
    """Calls back into the runner mid-turn, like a parent sending a message."""

    mutates_world = True

    def __init__(self, runner: SubAgentRunner, target: dict, text: str) -> None:
        self._runner = runner
        self._target = target
        self._text = text
        self.results: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "ping"

    @property
    def description(self) -> str:
        return "send an agent message to the child"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        result = await self._runner.send_message(self._target["id"], self._text)
        self.results.append(result)
        return ToolResult(success=True, output="pinged")


# ---- runner helpers ----------------------------------------------------------


def _runner(tmp_path, engines, **over) -> SubAgentRunner:
    kwargs = {
        "app_config": object(),
        "engine_factory": engines,
        "build_tools": lambda _workdir: ToolRegistry(),
        "ledger_root": tmp_path,
        "workspace": str(tmp_path),
        "timeout_seconds": 30.0,
        "settings": AIConfig(stop_policy="natural"),
        "notices": SteeringQueue(),
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


async def _settle(runner: SubAgentRunner, run_id: str) -> BackgroundTask:
    for _ in range(400):
        for row in runner.task_list():
            if row.run_id == run_id and row.status != "running":
                return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} never settled")


def _user_texts(session: SessionData) -> list[str]:
    return [m.content for m in session.messages if m.role == "user"]


# ---- T8: a running turn takes the message at its next boundary ---------------


@pytest.mark.asyncio
async def test_send_to_a_running_turn_is_read_at_the_next_step(tmp_path):
    engines = _Engines()
    engines.push(_call("c1", "ping"), _text("done"))
    tools: dict[str, ToolRegistry] = {}
    target = {"id": ""}
    runner = _runner(
        tmp_path, engines, build_tools=lambda _workdir: tools["registry"]
    )
    ping = _PingTool(runner, target, "hi from the parent")
    tools["registry"] = _registry(ping)

    run_id = runner.spawn_background("first task", name="peer", emit=lambda _e: None)
    target["id"] = run_id
    handle = runner._handles[run_id]

    row = await _settle(runner, run_id)

    assert row.status == "completed"
    assert ping.results == [("queued", run_id)]
    assert any("[agent-message] hi from the parent" in text for text in _user_texts(handle.session))
    # The message was absorbed by the running turn: no second turn was started.
    assert handle.session.turn_count == 1


def _registry(*tools: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


# ---- T9: an idle child is woken into a new turn ------------------------------


@pytest.mark.asyncio
async def test_send_to_an_idle_child_starts_one_turn_and_one_notice(tmp_path):
    engines = _Engines()
    engines.push(_text("did the first"), _text("did the second"))
    events: list[BackgroundTaskDone] = []
    runner = _runner(tmp_path, engines)
    notices: SteeringQueue = runner._notices  # type: ignore[assignment]

    run_id = runner.spawn_background("first task", name="peer", emit=events.append)
    handle = runner._handles[run_id]
    await _settle(runner, run_id)
    assert len(events) == 1
    notices.drain()

    status, rid = await runner.send_message(run_id, "second task")
    assert (status, rid) == ("running", run_id)
    row = await _settle(runner, run_id)

    assert row.status == "completed"
    assert handle.session.turn_count == 2
    replies = [m.content for m in handle.session.messages if m.role == "assistant"]
    assert any("did the second" in text for text in replies)
    # The previous task and its reply are still in the conversation.
    texts = _user_texts(handle.session)
    assert any("first task" in text for text in texts)
    assert any("second task" in text for text in texts)
    fresh = notices.drain()
    assert len(fresh) == 1 and "did the second" in fresh[0]
    assert len(events) == 2


# ---- T10: interrupt keeps the handle, sends no notice, resumes ---------------


@pytest.mark.asyncio
async def test_interrupt_cuts_the_turn_and_a_resumed_send_runs_again(tmp_path):
    entered = asyncio.Event()
    engines = _Engines()
    engines.push(_text("first done"), _hang(entered))
    events: list[BackgroundTaskDone] = []
    runner = _runner(tmp_path, engines)

    run_id = runner.spawn_background("first task", name="cut", emit=events.append)
    handle = runner._handles[run_id]
    await _settle(runner, run_id)
    notices: SteeringQueue = runner._notices  # type: ignore[assignment]
    notices.drain()

    status, rid = await runner.send_message(run_id, "second task")
    assert status == "running"
    await asyncio.wait_for(entered.wait(), timeout=5)

    previous, after = await runner.interrupt(run_id)

    assert (previous, after) == ("running", "interrupted")
    assert handle.status == "interrupted"
    assert handle.turn_task is None
    row = runner._background[run_id]
    assert row.status == "interrupted"
    # Interrupted is not a completion: nothing is announced.
    assert notices.drain() == []
    assert len(events) == 1  # only the first turn settled
    # The cut turn's history is still there and its open-turn marker is gone.
    assert any("second task" in text for text in _user_texts(handle.session))
    assert handle.session.open_turn is None
    assert handle.loop is None

    status, rid = await runner.resume(run_id)
    assert (status, rid) == ("idle", run_id)
    status, rid = await runner.send_message(run_id, "third task")
    assert status == "running"
    row = await _settle(runner, run_id)

    assert row.status == "completed"
    assert any("third task" in text for text in _user_texts(handle.session))


# ---- T11: resume across instances (sidecar + session on disk) ----------------


@pytest.mark.asyncio
async def test_resume_from_disk_in_a_fresh_runner(tmp_path):
    first = _runner(tmp_path, _Engines())
    first._engine_factory.push(_text("ran before"))  # type: ignore[union-attr]
    run_id = first.spawn_background("first task", name="persisted", emit=lambda _e: None)
    await _settle(first, run_id)
    session_id = first._handles[run_id].session.session_id
    assert (tmp_path / ".ai-coding" / "agents" / "sessions" / f"{session_id}.json").is_file()

    second = _runner(tmp_path, _Engines())
    second._engine_factory.push(_text("ran after"))  # type: ignore[union-attr]
    status, rid = await second.resume(run_id)
    assert (status, rid) == ("idle", run_id)
    assert run_id in second._handles

    status, rid = await second.send_message(run_id, "continue please")
    assert status == "running"
    row = await _settle(second, run_id)

    assert row.status == "completed"
    texts = _user_texts(second._handles[run_id].session)
    assert any("first task" in text for text in texts)
    assert any("continue please" in text for text in texts)


# ---- T12: close drops the handle; resume reopens it -------------------------


@pytest.mark.asyncio
async def test_close_then_resume_reopens_the_same_child(tmp_path):
    engines = _Engines()
    engines.push(_text("before close"), _text("after resume"))
    runner = _runner(tmp_path, engines)
    run_id = runner.spawn_background("first task", name="closable", emit=lambda _e: None)
    await _settle(runner, run_id)
    handle = runner._handles[run_id]
    notices: SteeringQueue = runner._notices  # type: ignore[assignment]
    notices.drain()

    assert await runner.close(run_id) is True
    assert run_id not in runner._handles
    assert runner._background[run_id].status == "cancelled"
    assert notices.drain() == []  # closing is not a completion

    status, rid = await runner.resume(run_id)
    assert (status, rid) == ("idle", run_id)
    reopened = runner._handles[run_id]
    assert reopened.session.session_id == handle.session.session_id

    status, rid = await runner.send_message(run_id, "again")
    assert status == "running"
    row = await _settle(runner, run_id)
    assert row.status == "completed"
    assert any("again" in text for text in _user_texts(reopened.session))


# ---- T13: the plan posture hides all three ----------------------------------


class _StubRunner:
    """Only the three runner methods the tools call (plus addressing)."""

    def __init__(self) -> None:
        self.send_result = ("running", "run_1")
        self.interrupt_result = ("running", "interrupted")
        self.resume_result = ("idle", "run_1")
        self.sent: list[tuple[str, str]] = []
        self.interrupted: list[str] = []
        self.resumed: list[str] = []

    def resolve_run_id(self, prefix: str) -> str | None:
        return None

    async def send_message(self, target, text):
        self.sent.append((target, text))
        return self.send_result

    async def interrupt(self, target):
        self.interrupted.append(target)
        return self.interrupt_result

    async def resume(self, target):
        self.resumed.append(target)
        return self.resume_result


def _control_tools(runner=None) -> list[BaseTool]:
    runner = runner or _StubRunner()
    return [
        SendMessageTool(runner),
        InterruptAgentTool(runner),
        ResumeAgentTool(runner),
    ]


def _loop_with(tmp_path, tools: list[BaseTool]) -> AgentLoop:
    class _NeverEngine(ChatEngine):
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            raise AssertionError("catalogue tests never run a turn")

    return AgentLoop(
        _NeverEngine(),
        SessionService(root=tmp_path / "sessions"),
        _registry(*tools),
        gate=PermissionGate(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )


def _plan_rules() -> tuple[Rule, ...]:
    from ai_coding.agents.registry import builtin_catalog

    return builtin_catalog().rules("plan")


def test_the_three_names_hide_under_the_plan_posture(tmp_path):
    loop = _loop_with(tmp_path, _control_tools())

    hidden = loop._surface_tools("plan", False, (), _plan_rules())
    assert [tool.name for tool in hidden] == []

    visible = loop._surface_tools("normal", False, (), ())
    assert {tool.name for tool in visible} == {
        "send_message",
        "interrupt_agent",
        "resume_agent",
    }


def test_a_subagent_session_deny_also_hides_the_three(tmp_path):
    loop = _loop_with(tmp_path, _control_tools())
    deny = (Rule(action="subagent", resource="*", effect=Effect.DENY),)

    assert loop._surface_tools("normal", False, deny) == []


# ---- tool shapes and results -------------------------------------------------


def test_the_three_tools_are_shaped_and_gated():
    tools = _control_tools()

    assert [tool.name for tool in tools] == [
        "send_message",
        "interrupt_agent",
        "resume_agent",
    ]
    for tool in tools:
        assert tool.mutates_world is True
        assert tool.permission_name() == "subAgent"

    send = tools[0].params_json_schema
    assert set(send["properties"]) == {"target", "message"}
    assert send["required"] == ["target", "message"]
    assert set(tools[1].params_json_schema["properties"]) == {"target"}
    assert tools[2].params_json_schema["required"] == ["agent_id"]


@pytest.mark.asyncio
async def test_send_message_renders_queued_and_started():
    runner = _StubRunner()

    runner.send_result = ("queued", "run_1")
    queued = await SendMessageTool(runner).execute(
        {"target": "run_1", "message": "  hello  "}
    )
    assert queued.success is True
    assert queued.output == "message queued to run_1"
    assert queued.data == {"target": "run_1", "status": "queued"}
    assert runner.sent == [("run_1", "hello")]

    runner.send_result = ("running", "run_1")
    started = await SendMessageTool(runner).execute(
        {"target": "run_1", "message": "hello"}
    )
    assert started.output == "message started a turn on run_1"
    assert started.data == {"target": "run_1", "status": "running"}


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["", "   ", "x" * 2001])
async def test_send_message_rejects_an_empty_or_oversized_message(message):
    result = await SendMessageTool(_StubRunner()).execute(
        {"target": "run_1", "message": message}
    )

    assert result.success is False
    assert result.error == "invalid_message"


@pytest.mark.asyncio
async def test_send_message_reports_an_unknown_target():
    runner = _StubRunner()
    runner.send_result = ("unknown", "")

    result = await SendMessageTool(runner).execute(
        {"target": "nope", "message": "hi"}
    )

    assert result.success is False
    assert result.error == "unknown_agent"
    assert result.data == {"target": "nope", "status": "unknown"}


@pytest.mark.asyncio
async def test_interrupt_renders_the_transition_and_never_fails():
    runner = _StubRunner()

    result = await InterruptAgentTool(runner).execute({"target": "run_1"})

    assert result.success is True
    assert result.output == "agent run_1: running -> interrupted"
    assert result.data == {
        "target": "run_1",
        "previous_status": "running",
        "status": "interrupted",
    }

    # Codex semantics: "already dead" is still a success.
    runner.interrupt_result = ("not_found", "not_found")
    missing = await InterruptAgentTool(runner).execute({"target": "gone"})
    assert missing.success is True
    assert missing.data["status"] == "not_found"


@pytest.mark.asyncio
async def test_resume_renders_idle_and_refuses_a_missing_agent():
    runner = _StubRunner()

    result = await ResumeAgentTool(runner).execute({"agent_id": "run_1"})
    assert result.success is True
    assert result.output == "agent run_1: idle"
    assert result.data == {"agent_id": "run_1", "status": "idle"}

    runner.resume_result = ("not_found", "run_9")
    missing = await ResumeAgentTool(runner).execute({"agent_id": "run_9"})
    assert missing.success is False
    assert missing.error == "not_found"
    assert missing.data == {"agent_id": "run_9", "status": "not_found"}


# ---- T14: two sends, one turn; two handles, bounded by the pool --------------


@pytest.mark.asyncio
async def test_two_sends_to_one_idle_handle_start_exactly_one_turn(tmp_path):
    engines = _Engines()
    engines.push(_text("first"), _text("second"))
    runner = _runner(tmp_path, engines)

    run_id = runner.spawn_background("first task", name="once", emit=lambda _e: None)
    handle = runner._handles[run_id]
    await _settle(runner, run_id)
    runner._notices.drain()  # type: ignore[union-attr]

    first, _ = await runner.send_message(run_id, "one")
    second, _ = await runner.send_message(run_id, "two")

    assert first == "running" and second == "queued"
    assert handle.turn_task is not None
    assert list(handle.mailbox) == ["two"]
    # One task owns the run id; the second message did not start a rival turn.
    assert len([task for task in runner._tasks.values() if not task.done()]) == 1

    row = await _settle(runner, run_id)
    assert row.status == "completed"
    # Both messages ran, as turns -- never concurrently.
    assert handle.session.turn_count >= 2


@pytest.mark.asyncio
async def test_a_followup_turn_cannot_start_past_the_pool_cap(tmp_path):
    engines = _Engines()
    engines.push(_text("a1"), _text("a2"), _text("b1"), _text("b2"))
    runner = _runner(tmp_path, engines, max_concurrent=1)

    first = runner.spawn_background("first", name="a", emit=lambda _e: None)
    await _settle(runner, first)
    second = runner.spawn_background("second", name="b", emit=lambda _e: None)
    await _settle(runner, second)
    runner._notices.drain()  # type: ignore[union-attr]

    status, _ = await runner.send_message(first, "more")
    assert status == "running"

    with pytest.raises(SubAgentPoolFull):
        await runner.send_message(second, "more")

    await _settle(runner, first)


@pytest.mark.asyncio
async def test_close_of_an_unknown_target_is_false(tmp_path):
    runner = _runner(tmp_path, _Engines())
    assert await runner.close("nope") is False


# ---- P2-B3: the settlement is written down and the idle loop is woken -----


class _RecordingQueue:
    """A ``SteeringQueue`` stand-in that records the wake flag."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, bool]] = []

    def put(self, text: str, *, wake: bool = False) -> bool:
        self.puts.append((text, wake))
        return True

    def drain(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_a_settlement_is_persisted_and_queued_with_wake(tmp_path):
    from ai_coding.core.notice_store import NoticeStore

    queue = _RecordingQueue()
    store = NoticeStore.for_workspace(tmp_path)
    runner = _runner(tmp_path, _Engines(), notices=queue, notice_store=store)
    run_id = runner.spawn_background(
        "do it", name="probe", emit=lambda _e: None, origin_session="sess-1"
    )
    await _settle(runner, run_id)

    assert queue.puts, "the running loop's lane must still be used"
    line, wake = queue.puts[0]
    assert wake is True, "an idle loop may start a turn for a settlement"
    assert [row["text"] for row in store.pending("sess-1")] == [line]


@pytest.mark.asyncio
async def test_an_empty_origin_session_writes_no_disk_notice(tmp_path):
    """A child without a spawning session has no mailbox to leave a line in."""
    from ai_coding.core.notice_store import NoticeStore

    store = NoticeStore.for_workspace(tmp_path)
    queue = _RecordingQueue()
    runner = _runner(tmp_path, _Engines(), notices=queue, notice_store=store)
    run_id = runner.spawn_background("do it", name="probe", emit=lambda _e: None)
    await _settle(runner, run_id)

    assert queue.puts  # the in-memory lane still speaks
    assert list((tmp_path / ".ai-coding" / "notices").glob("*.jsonl")) == []


@pytest.mark.asyncio
async def test_a_disk_resume_keeps_the_parent_rule_layer(tmp_path):
    """The sidecar's rule layer is what a resumed child is held to."""
    from ai_coding.security.permissions import rule_to_dict

    rule = Rule(action="edit", resource="*.env", effect=Effect.DENY)
    engines = _Engines()
    engines.push(_text("guarded"))
    first = _runner(tmp_path, engines)
    run_id = first.spawn_background(
        "guarded", name="guarded", parent=ParentContext(permissions=(rule,)),
        emit=lambda _e: None,
    )
    await _settle(first, run_id)

    second = _runner(tmp_path, _Engines())
    status, rid = await second.resume(run_id)

    assert (status, rid) == ("idle", run_id)
    assert [rule_to_dict(item) for item in second._handles[run_id].parent_permissions] == [
        rule_to_dict(rule)
    ]
