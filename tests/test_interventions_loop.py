"""TDD (13 §2.1): the ladder runs inside the loop and survives the turn boundary.

脚本引擎与循环构造沿用 ``tests/test_agent_loop_react.py`` 的形状（该仓测试自包含）。
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.task import TaskState, TodoItem
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _Flaky(BaseTool):
    """Always fails: the ladder needs a turn where nothing ever works."""

    name = "flaky"
    parallel_safe = True

    @property
    def description(self) -> str:
        return "a tool that always fails"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=False, output="boom", error="nonzero_exit")


class _ScriptedEngine(ChatEngine):
    """Returns pre-scripted steps and records every request it received."""

    def __init__(self, steps: list) -> None:
        self._steps = list(steps)
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        if not self._steps:
            raise AssertionError("engine called more times than the script allows")
        item = self._steps.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self) -> None:
        return None


def _call(call_id: str) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name="flaky", arguments="{}")],
        finish_reason="tool_calls",
    )


def _text(text: str) -> StepResult:
    return StepResult(text=text, finish_reason="stop")


def _loop(engine: ChatEngine, sessions: SessionService, **settings) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Flaky())
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(**settings),
        workspace=".",
    )


@pytest.mark.asyncio
async def test_a_turn_cut_mid_window_retries_the_same_level_next_turn(tmp_path):
    engine = _ScriptedEngine([
        _call("c1"), _call("c2"), _call("c3"),   # 两次全败 → L1；窗口内再败 → L2
        _text("先停一下"),                        # L2 窗口中途，模型收尾 → 回合 1 结束
        _call("c4"), _call("c5"),                 # 回合 2 再次全败 → 触发
        _text("again"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()

    await loop.process_input(session, "go")
    first = session.intervention_state["strategy"]
    assert (first["level"], first["outcome"]) == (2, "unfinished")   # 回合边界 ≠ failed

    await loop.process_input(session, "again")
    second = session.intervention_state["strategy"]
    assert second["level"] == 2                                       # 同级重试，不越级
    nudges = [
        m.content or "" for m in session.messages
        if m.synthetic and "strategy-gate" in (m.content or "")
    ]
    assert "直接拦截" in nudges[-1]                                   # L2 的武装文案
    persisted = sessions.load(session.session_id)
    assert persisted.intervention_state["strategy"]["level"] == 2     # 搭车落盘、读回存活


@pytest.mark.asyncio
async def test_completing_the_task_clears_the_ledger(tmp_path):
    engine = _ScriptedEngine([_text("done")])
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions)
    session = sessions.create()
    session.task = TaskState(
        goal="g", items=[TodoItem(id="1", content="c", status="completed")]
    )
    session.intervention_state = {
        "task": {"level": 1, "at": [1, 3], "outcome": "unfinished", "fires": 1, "ref": {}}
    }

    await loop.process_input(session, "go")

    assert session.intervention_state is None       # items 全终态 → 账本清空
    persisted = sessions.load(session.session_id)
    assert persisted.intervention_state is None     # 条件写：清空后文件里没有键


# ---------------- 20 §批次3: a validation refusal is not a failure


class _Ok(BaseTool):
    """One success, so the stall clause (successful_calls == 0) stays out."""

    name = "ok"
    parallel_safe = True

    @property
    def description(self) -> str:
        return "a tool that works"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=True, output="fine")


class _Refuser(BaseTool):
    """present_plan's verdict class: refuse until the list is written."""

    name = "present_plan"
    parallel_safe = False

    @property
    def description(self) -> str:
        return "refuses to present an empty plan"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=False, output="计划尚未写成条目", error="no_plan_items")


def _call_named(call_id: str, name: str) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments="{}")],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_a_validation_refusal_does_not_escalate_the_l1_window(tmp_path):
    """24d7109b replay: two real failures armed L1; the refusal round (a
    no_plan_items verdict) used to keep the burn alive, so the closed window
    read "failed" and L2 ordered a second nudge the model spent on a duplicate
    report.  A refusal resets the streak (turn_books), the window closes
    "helped", and the turn carries on."""
    engine = _ScriptedEngine([
        _call_named("c1", "ok"),            # a success somewhere in the turn
        _call_named("c2", "flaky"),         # all-fail 1
        _call_named("c3", "flaky"),         # all-fail 2 -> L1 fires
        _call_named("c4", "present_plan"),  # the validation refusal
        _text("继续修"),
    ])
    sessions = SessionService(tmp_path / "sessions")
    registry = ToolRegistry()
    registry.register(_Flaky())
    registry.register(_Ok())
    registry.register(_Refuser())
    loop = AgentLoop(
        engine,
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=".",
    )
    session = sessions.create()

    await loop.process_input(session, "go")

    strategy = session.intervention_state["strategy"]
    assert strategy["outcome"] == "helped"
    assert strategy["level"] == 1
    nudges = [
        m.content or ""
        for m in session.messages
        if m.synthetic and "strategy-gate" in (m.content or "")
    ]
    assert len(nudges) == 1
    assert "直接拦截" not in nudges[0]  # L2's armed text never lands


# ---- B1/B2/B5: the search ladder end to end ----


class _Reader(BaseTool):
    """A retrieval tool: its success is a "look", not an action."""

    name = "read"
    parallel_safe = True

    @property
    def description(self) -> str:
        return "reads a file"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None) -> ToolResult:
        return ToolResult(success=True, output="file body")


def _read_call(call_id: str) -> StepResult:
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name="read", arguments="{}")],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_a_look_only_turn_gets_the_static_check_output(tmp_path, monkeypatch):
    """B1's whole path: streak -> ladder -> harness runs the command -> injection.

    Not a unit test of any one piece: a real AgentLoop drives a real tool, the
    real policy decides, and the synthetic message the model would read is
    asserted to carry both the gate text and the command's own output.

    The run itself is opt-in (``AI_CODING_STATIC_CHECK=1``) because the commands
    can come from a repository's tracked config -- see
    ``core/static_check.py``.
    """
    from ai_coding.core.events import RecordingObserver
    from ai_coding.core.static_check import ENABLE_ENV
    from ai_coding.tools.registry import ToolRegistry

    monkeypatch.setenv(ENABLE_ENV, "1")

    engine = _ScriptedEngine(
        [
            _read_call("c1"), _read_call("c2"), _read_call("c3"), _read_call("c4"),
            _read_call("c5"), _read_call("c6"), _read_call("c7"), _read_call("c8"),
            _read_call("c9"), _read_call("c10"), _read_call("c11"), _read_call("c12"),
            _text("done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_Reader())
    observer = RecordingObserver()
    loop = AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(
            max_tool_iterations=13,
            static_check={"commands": ["echo STATIC-CHECK-ANSWER"]},
        ),
        observer=observer,
        workspace=str(tmp_path),
    )
    session = loop.sessions.create()

    await loop.process_input(session, "find the thing")

    nudges = [
        m.content or ""
        for m in session.messages
        if m.synthetic and "search-gate" in (m.content or "")
    ]
    levels = [e.level for e in observer.events if type(e).__name__ == "SearchStalled"]
    assert levels[0] == 1, "the first rung changes the approach, not the tool"
    assert levels == sorted(levels), "the ladder climbs, it never goes back down"
    assert 2 in levels, "the turn kept looking, so it must reach the static check"
    static_nudges = [n for n in nudges if "STATIC-CHECK-ANSWER" in n]
    assert static_nudges, "L2 must hand over the real output the harness produced"
    # P1-3 (review): the output must not land on the turn's last iteration -- the
    # model has to get a step in which it can read it.
    assert any(
        message.synthetic and "search-gate" in (message.content or "")
        for message in session.messages[:-1]
    ), "a nudge must never be the last message of the turn"


@pytest.mark.asyncio
async def test_the_static_check_output_is_readable_under_the_default_budget(
    tmp_path, monkeypatch
):
    """The default budget is 10 iterations; L2 must leave room to consume it."""
    from ai_coding.core.events import RecordingObserver
    from ai_coding.core.static_check import ENABLE_ENV
    from ai_coding.tools.registry import ToolRegistry

    monkeypatch.setenv(ENABLE_ENV, "1")

    engine = _ScriptedEngine(
        [_read_call(f"d{i}") for i in range(1, 10)] + [_text("done")]
    )
    registry = ToolRegistry()
    registry.register(_Reader())
    observer = RecordingObserver()
    loop = AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(static_check={"commands": ["echo STATIC-CHECK-ANSWER"]}),
        observer=observer,
        workspace=str(tmp_path),
    )
    session = loop.sessions.create()

    await loop.process_input(session, "find the thing")

    events = observer.events
    l2_at = next(
        e.iteration
        for e in events
        if type(e).__name__ == "SearchStalled" and e.level == 2
    )
    later_steps = [
        e.index for e in events if type(e).__name__ == "IterationStarted" and e.index > l2_at
    ]
    assert later_steps, f"L2 landed at iteration {l2_at} with no step left to read it"
