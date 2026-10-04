"""Background sub-agents (R1-2, TDD): delegation without hostage-taking.

A synchronous ``subAgent`` call burns the parent's whole turn budget waiting
(C2 measured 300s for zero progress).  With ``background=true`` the tool
returns a run id at once; the child runs on, and its completion lands as an
event plus a table row — never as a second tool message (strict pairing
allows exactly one tool message per call).
"""

import asyncio

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.core.events import BackgroundTaskDone
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import BackgroundTask
from ai_coding.tools.base import ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.subagent_tool import SubAgentTool


class _Engine(ChatEngine):
    def __init__(self, text="did it", hang=False, usage=None):
        self._text = text
        self._hang = hang
        self._usage = usage

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if self._hang:
            await asyncio.sleep(30)
        return StepResult(text=self._text, finish_reason="stop", usage=self._usage)

    async def aclose(self):
        return None


def _runner(engine, **over):
    kwargs = {
        "app_config": object(),
        "engine_factory": lambda: engine,
        "build_tools": lambda wd: ToolRegistry(),
        "timeout_seconds": 600.0,
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


@pytest.mark.asyncio
async def test_spawn_returns_at_once_while_child_runs_on():
    runner = _runner(_Engine(hang=True))
    run_id = runner.spawn_background("sleep a while", name="sleeper",
                                     emit=lambda e: None)
    assert isinstance(run_id, str) and run_id
    rows = runner.task_list()
    assert len(rows) == 1 and rows[0].status == "running"
    assert rows[0].name == "sleeper"
    assert runner.cancel(run_id) is True
    for _ in range(100):
        if runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)
    assert runner.task_list()[0].status == "cancelled"


@pytest.mark.asyncio
async def test_saturated_pool_rejects_instead_of_queueing_silently():
    runner = _runner(_Engine(), max_concurrent=1)
    await runner._semaphore.acquire()
    try:
        with pytest.raises(RuntimeError, match="concurrency exhausted"):
            runner.spawn_background("x", emit=lambda e: None)
    finally:
        runner._semaphore.release()
    assert runner.task_list() == []


@pytest.mark.asyncio
async def test_completion_lands_as_event_with_summary():
    seen: list = []
    runner = _runner(_Engine(text="all green"))
    run_id = runner.spawn_background("do it", name="probe", emit=seen.append)
    for _ in range(200):
        if runner.task_list() and runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)
    row = runner.task_list()[0]
    assert row.status == "completed"
    assert len(seen) == 1 and isinstance(seen[0], BackgroundTaskDone)
    event = seen[0]
    assert event.run_id == run_id and event.name == "probe"
    assert event.status == "completed" and "all green" in event.summary
    assert event.truncated is False


@pytest.mark.asyncio
async def test_child_spend_is_recorded_on_the_row():
    seen: list = []
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    runner = _runner(_Engine(text="done", usage=usage))
    runner.spawn_background("do it", emit=seen.append)
    for _ in range(200):
        if runner.task_list()[0].status != "running":
            break
        await asyncio.sleep(0.02)
    assert runner.task_list()[0].usage == usage


@pytest.mark.asyncio
async def test_cancel_unknown_id_is_false():
    runner = _runner(_Engine())
    assert runner.cancel("nope") is False


def test_background_task_row_round_trips():
    row = BackgroundTask(run_id="abc123", name="n", task="t", status="running",
                         started=1.0)
    assert BackgroundTask.from_dict(row.to_dict()).run_id == "abc123"


@pytest.mark.asyncio
async def test_tool_background_param_spawns_and_returns_a_handle():
    seen_observers: list = []

    class _CannedRunner:
        timeout_seconds = 600.0
        context_mode = "full"

        async def run(self, task, name="sub-agent", **kwargs):
            raise AssertionError("sync path must not run")

        def spawn_background(self, task, name="sub-agent", **kwargs):
            seen_observers.append(kwargs.get("observer"))
            return "run-1"

    tool = SubAgentTool(_CannedRunner())
    result = await tool.execute({"task": "do it", "background": True},
                                ToolContext(emit=lambda e: None))
    assert result.success is True
    assert "run-1" in result.output and result.data["run_id"] == "run-1"
    assert result.data["background"] is True
    assert seen_observers[0] is not None  # child events still fan in


@pytest.mark.asyncio
async def test_tool_defaults_to_the_synchronous_path():
    from ai_coding.domain.subagent import SubagentResult

    class _CannedRunner:
        timeout_seconds = 600.0
        context_mode = "full"

        async def run(self, task, name="sub-agent", **kwargs):
            return SubagentResult(status="completed", conclusion="done", iterations=1)

    tool = SubAgentTool(_CannedRunner())
    result = await tool.execute({"task": "do it"}, ToolContext(emit=lambda e: None))
    assert result.success is True and "done" in result.output


def test_scoped_completion_block_is_transcript_safe():
    from ai_coding.ui.blocks import blocks_from_event

    event = BackgroundTaskDone(run_id="abc123", name="probe", status="timeout",
                               summary="build [k=v] hung", truncated=True,
                               origin_session="s1")
    block = blocks_from_event(event)
    assert block is not None
    assert "probe" in block.text and "build [k=v] hung" in block.text


@pytest.mark.asyncio
async def test_table_prunes_oldest_terminal_rows_first(monkeypatch):
    import ai_coding.core.subagent as subagent_mod

    monkeypatch.setattr(subagent_mod, "_MAX_BACKGROUND_ROWS", 2)
    runner = _runner(_Engine(text="done"))

    async def settle():
        for _ in range(200):
            if all(r.status != "running" for r in runner.task_list()):
                return
            await asyncio.sleep(0.02)

    runner.spawn_background("one", name="two", emit=lambda e: None)
    await settle()
    runner.spawn_background("two", name="three", emit=lambda e: None)
    await settle()
    runner.spawn_background("three", name="four", emit=lambda e: None)
    await settle()
    rows = runner.task_list()
    assert len(rows) == 2
    assert [r.name for r in rows] == ["three", "four"]
    assert all(r.status != "running" for r in rows)
