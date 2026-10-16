"""P0-2: a harness fact the run learned reaches the model.

``[steering]`` is the user's own voice by definition, and the replay renders it
as the operator's words.  A background sub-agent completing is not that: it is a
fact about a tool call the model itself made, produced from a task, long after
the turn that started it may have ended.  It gets its own lane -- the same
bounded FIFO, a sibling drain point, a different marker -- because the two
differ in exactly the ways that matter: a steering line resets the stall
ladder (``TurnBooks.note_steering``), and must never be attributed to the user.

The tests drive a real ``AgentLoop``; only the model is faked.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import InputSteered, RecordingObserver
from ai_coding.core.steering import SteeringQueue
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Ask for one tool on the first step, then speak."""

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            return StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="poke", arguments="{}")],
                finish_reason="tool_calls",
                usage=None,
                reasoning=None,
            )
        return StepResult(
            text="好", tool_calls=(), finish_reason="stop", usage=None, reasoning=None
        )


class _PlainEngine(ChatEngine):
    """Speaks immediately: one iteration, therefore one drain."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="说完了", finish_reason="stop", usage=None, reasoning=None)


class _Poke(BaseTool):
    parallel_safe = False

    @property
    def name(self) -> str:
        return "poke"

    @property
    def description(self) -> str:
        return "poke"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="poked")


class _Sessions:
    def save(self, session: SessionData) -> None:
        return None

    def mark_pending_approval(
        self, session_id: str, request: dict[str, Any] | None
    ) -> None:
        return None


def _loop(
    notices: SteeringQueue | None,
    engine: ChatEngine,
    *,
    observer: RecordingObserver | None = None,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Poke())
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=3, stop_policy="natural"),
        observer=observer,
        notices=notices,
    )


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_a_fact_becomes_a_persisted_synthetic_message():
    notices = SteeringQueue()
    notices.put("[子任务完成] probe（abc123def456）：跑完了")
    session = _session()

    await _loop(notices, _Engine()).process_input(session, "派个后台任务")

    facts = [m for m in session.messages if (m.content or "").startswith("[notice] ")]
    assert len(facts) == 1
    assert "[子任务完成] probe（abc123def456）：跑完了" in facts[0].content
    # synthetic is the flag that keeps undo, the mention rebuild and the
    # compaction anchor from treating it as something the operator said.
    assert facts[0].synthetic is True


@pytest.mark.asyncio
async def test_a_fact_is_never_attributed_to_the_operator():
    """The lane exists because of this: ``[steering]`` is rendered as the user's
    own words on replay, so a harness fact wearing that marker would be a lie in
    the permanent record."""
    notices = SteeringQueue()
    notices.put("[子任务失败] probe：挂了")
    session = _session()

    await _loop(notices, _Engine()).process_input(session, "派个后台任务")

    contents = [m.content or "" for m in session.messages]
    assert not any("[steering]" in content for content in contents)
    assert any("[notice] [子任务失败] probe：挂了" in content for content in contents)


@pytest.mark.asyncio
async def test_a_fact_is_not_reported_as_user_steering():
    """``InputSteered`` is the public seam of the steering drain, and that drain
    calls ``TurnBooks.note_steering`` -- which restarts the failure streak.  A
    finished background task is not user intervention, so the streak must stand
    (the streak itself lives inside the turn, which no caller can reach)."""
    notices = SteeringQueue()
    notices.put("[子任务完成] probe：跑完了")
    observer = RecordingObserver()

    await _loop(notices, _Engine(), observer=observer).process_input(_session(), "派个后台任务")

    assert observer.of_type(InputSteered) == []


@pytest.mark.asyncio
async def test_the_model_sees_the_fact_in_the_same_iteration():
    notices = SteeringQueue()
    notices.put("[子任务完成] probe：跑完了")
    engine = _Engine()

    await _loop(notices, engine).process_input(_session(), "派个后台任务")

    second_request = [m.content or "" for m in engine.requests[1]]
    assert any("[notice] [子任务完成]" in content for content in second_request)


@pytest.mark.asyncio
async def test_a_fact_that_lands_after_the_turn_waits_for_the_next_one():
    """The common case: the child finishes turns after the spawning turn ended.
    The next turn is where the operator is told, and that is the same residual
    semantics steering has -- nothing is dropped, it waits."""
    notices = SteeringQueue()
    first = _session()

    await _loop(notices, _PlainEngine()).process_input(first, "派个后台任务")
    assert not any("[notice]" in (m.content or "") for m in first.messages)

    # ... and now the child settles, long after that turn ended.
    notices.put("[子任务完成] probe：跑完了")
    second = _session()

    await _loop(notices, _PlainEngine()).process_input(second, "它跑完了吗")

    assert any("[notice] [子任务完成]" in (m.content or "") for m in second.messages)


@pytest.mark.asyncio
async def test_leftovers_stay_queued_when_a_turn_only_drains_once():
    notices = SteeringQueue(max_messages=1)
    notices.put("第一条")
    notices.put("第二条")

    await _loop(notices, _PlainEngine()).process_input(_session(), "派个后台任务")

    assert notices.pending == 1


@pytest.mark.asyncio
async def test_no_source_means_no_behaviour_change():
    plain, queued = _Engine(), _Engine()

    await _loop(None, plain).process_input(_session(), "派个后台任务")
    await _loop(SteeringQueue(), queued).process_input(_session(), "派个后台任务")

    assert [m.content for m in plain.requests[1]] == [m.content for m in queued.requests[1]]
