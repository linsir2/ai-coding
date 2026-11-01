"""C-5b edges: ambiguity, resume-running no-op, and pool saturation.

These are the addressing/scheduling rules the tools promise; they do not need a
model at all (the runner is driven directly).
"""

import asyncio

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import SubAgentPoolFull
from ai_coding.tools.agents_tool import (
    CloseAgentTool,
    InterruptAgentTool,
    ResumeAgentTool,
    SendMessageTool,
)
from ai_coding.tools.base import ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Hang(ChatEngine):
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.entered.set()
        await asyncio.sleep(3600)
        return StepResult(text="never", finish_reason="stop")

    async def aclose(self) -> None:
        return None


def _runner(tmp_path, **over) -> SubAgentRunner:
    kwargs = {
        "app_config": object(),
        "engine_factory": lambda: _Hang(),
        "build_tools": lambda _workdir: ToolRegistry(),
        "ledger_root": tmp_path,
        "workspace": str(tmp_path),
        "timeout_seconds": 30.0,
        "settings": AIConfig(stop_policy="natural"),
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


def _ctx() -> ToolContext:
    return ToolContext(session_id="parent", call_id="c1")


@pytest.mark.asyncio
async def test_a_duplicate_name_is_reported_as_ambiguous(tmp_path):
    runner = _runner(tmp_path)
    first = runner.spawn_background("a", name="dup", emit=lambda _e: None)
    second = runner.spawn_background("b", name="dup", emit=lambda _e: None)
    for _ in range(100):
        if runner._handles.get(first) and runner._handles.get(second):
            break
        await asyncio.sleep(0.02)

    for tool in (
        SendMessageTool(runner),
        InterruptAgentTool(runner),
        ResumeAgentTool(runner),
        CloseAgentTool(runner),
    ):
        key = "agent_id" if tool.name in ("resume_agent", "close_agent") else "target"
        params = {key: "dup"}
        if tool.name == "send_message":
            params["message"] = "hi"
        result = await tool.execute(params, _ctx())
        assert result.success is False, tool.name
        assert result.error == "ambiguous_target", tool.name
        assert set((result.data or {}).get("candidates") or []) == {first, second}

    runner.cancel(first)
    runner.cancel(second)


@pytest.mark.asyncio
async def test_resume_of_a_running_agent_is_a_noop(tmp_path):
    engine = _Hang()
    runner = _runner(tmp_path, engine_factory=lambda: engine)
    run_id = runner.spawn_background("work", name="live", emit=lambda _e: None)
    await asyncio.wait_for(engine.entered.wait(), timeout=5)

    status, resolved = await runner.resume(run_id)

    assert (status, resolved) == ("running", run_id)
    assert runner._handles[run_id].status == "running"
    runner.cancel(run_id)


@pytest.mark.asyncio
async def test_a_full_pool_rejects_a_second_child(tmp_path):
    runner = _runner(tmp_path)
    runner._max_concurrent_n = 1
    runner._semaphore = asyncio.Semaphore(1)
    first = runner.spawn_background("one", name="one", emit=lambda _e: None)
    for _ in range(100):
        if runner._handles.get(first):
            break
        await asyncio.sleep(0.02)

    with pytest.raises(SubAgentPoolFull):
        runner.spawn_background("two", name="two", emit=lambda _e: None)

    # A message to the busy child still queues; it does not need a new slot.
    status, _ = await runner.send_message(first, "hello")
    assert status == "queued"
    runner.cancel(first)
