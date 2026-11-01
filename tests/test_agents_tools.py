"""C-5a: the Codex-shaped multi-agent tools over ``SubAgentRunner``.

Four names (``spawn_agent`` / ``wait_agent`` / ``list_agents`` / ``close_agent``)
map onto the runner's existing ``spawn_background`` / ``wait`` / ``task_list`` /
``cancel``.  These tests use a fake runner: no model, no network, no worktree.
"""

import asyncio
import time

import pytest

from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.subagent import BackgroundTask
from ai_coding.security.permissions import Effect, Rule
from ai_coding.tools.agents_tool import (
    CloseAgentTool,
    ListAgentsTool,
    SpawnAgentTool,
    WaitAgentTool,
)
from ai_coding.tools.base import ToolContext


class _FakeRunner:
    """Records the calls the four tools make and returns fixed values."""

    def __init__(self) -> None:
        self.spawns: list[dict] = []
        self.waits: list[tuple[str, int]] = []
        self.cancels: list[str] = []
        self.rows: list[BackgroundTask] = []
        self.wait_result: tuple[str, int] = ("completed", 1200)
        self.cancel_result = True
        self.spawn_error: Exception | None = None

    def spawn_background(self, task, name="sub-agent", **kwargs):
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawns.append({"task": task, "name": name, **kwargs})
        return "run_000000000001"

    async def wait(self, run_id, timeout_ms=30_000):
        self.waits.append((run_id, timeout_ms))
        return self.wait_result

    def task_list(self):
        return list(self.rows)

    def cancel(self, run_id):
        self.cancels.append(run_id)
        return self.cancel_result


def _ctx() -> ToolContext:
    return ToolContext(
        messages=(ChatMessage(role="user", content="parent view", timestamp="t"),),
        permissions=(Rule(action="edit", resource="*.env", effect=Effect.DENY),),
        session_id="sid",
        call_id="call_1",
    )


def _tools(runner) -> list:
    return [
        SpawnAgentTool(runner, worktree_manager="wt/1", isolate=False),
        WaitAgentTool(runner),
        ListAgentsTool(runner),
        CloseAgentTool(runner),
    ]


def test_four_tools_are_named_and_shaped():
    tools = _tools(_FakeRunner())

    assert [tool.name for tool in tools] == [
        "spawn_agent",
        "wait_agent",
        "list_agents",
        "close_agent",
    ]
    for tool in tools:
        # C-5a: the child never edits the parent workspace directly, and the
        # spawn/close pair writes the background table.
        assert tool.mutates_world is False
        assert tool.parallel_safe is False
        assert tool.defer is False

    spawn = tools[0].params_json_schema
    assert set(spawn["properties"]) == {"task", "name", "fork_turns"}
    assert spawn["required"] == ["task"]
    assert spawn["properties"]["fork_turns"]["enum"] == ["none", "all"]

    wait = tools[1].params_json_schema
    assert set(wait["properties"]) == {"agent_id", "timeout_ms"}
    assert wait["required"] == ["agent_id"]

    assert tools[2].params_json_schema == {"type": "object", "properties": {}}

    close = tools[3].params_json_schema
    assert set(close["properties"]) == {"agent_id"}
    assert close["required"] == ["agent_id"]


@pytest.mark.asyncio
async def test_spawn_defaults_to_a_task_only_child():
    runner = _FakeRunner()
    tool = SpawnAgentTool(runner, worktree_manager="wt/1", isolate=False)

    result = await tool.execute({"task": "count files"}, _ctx())

    assert result.success is True
    assert result.data == {
        "agent_id": "run_000000000001",
        "status": "running",
        "fork_turns": "none",
    }
    call = runner.spawns[0]
    assert call["task"] == "count files"
    assert call["name"] == "sub-agent"
    assert call["parent"] is None
    assert call["context_mode"] == "task_only"
    assert call["worktree_manager"] == "wt/1"
    assert call["isolate"] is False
    assert call["parent_span"] == "call_1"
    assert call["origin_session"] == "sid"


@pytest.mark.asyncio
async def test_spawn_with_fork_turns_all_forwards_the_parent_view():
    runner = _FakeRunner()
    tool = SpawnAgentTool(runner, worktree_manager="wt/1", isolate=False)
    ctx = _ctx()

    result = await tool.execute(
        {"task": "do it", "name": "helper", "fork_turns": "all"}, ctx
    )

    call = runner.spawns[0]
    assert call["name"] == "helper"
    assert call["context_mode"] == "full"
    assert call["parent"] is not None
    assert call["parent"].messages == tuple(ctx.messages)
    assert call["parent"].permissions == tuple(ctx.permissions)
    assert result.success is True
    assert result.data["fork_turns"] == "all"


@pytest.mark.asyncio
async def test_spawn_rejects_an_empty_task():
    result = await SpawnAgentTool(_FakeRunner()).execute({"task": "   "}, _ctx())

    assert result.success is False
    assert result.error == "missing_task"


@pytest.mark.asyncio
async def test_spawn_reports_an_exhausted_pool_as_agents_exhausted():
    runner = _FakeRunner()
    runner.spawn_error = RuntimeError("sub-agent concurrency exhausted: 4 busy")

    result = await SpawnAgentTool(runner).execute({"task": "t"}, _ctx())

    assert result.success is False
    assert result.error == "agents_exhausted"
    assert "4 busy" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "running", "completed"])
async def test_wait_agent_renders_each_status(status):
    runner = _FakeRunner()
    runner.wait_result = (status, 1200)
    tool = WaitAgentTool(runner)

    result = await tool.execute({"agent_id": "run_1", "timeout_ms": 2500}, _ctx())

    assert runner.waits == [("run_1", 2500)]
    assert result.data == {
        "agent_id": "run_1",
        "status": status,
        "elapsed_ms": 1200,
    }
    assert f"agent run_1: {status} (1200 ms)" in result.output
    assert result.success is (status != "unknown")


@pytest.mark.asyncio
async def test_wait_agent_clamps_and_defaults_timeout():
    runner = _FakeRunner()
    tool = WaitAgentTool(runner)

    await tool.execute({"agent_id": "a"}, _ctx())
    await tool.execute({"agent_id": "b", "timeout_ms": 50}, _ctx())
    await tool.execute({"agent_id": "c", "timeout_ms": 900_000}, _ctx())

    assert [timeout for _, timeout in runner.waits] == [30_000, 1_000, 600_000]


@pytest.mark.asyncio
async def test_list_agents_handles_an_empty_table():
    result = await ListAgentsTool(_FakeRunner()).execute({}, _ctx())

    assert result.success is True
    assert result.output == "no background agents"
    assert result.data == {"agents": []}


@pytest.mark.asyncio
async def test_list_agents_renders_two_rows():
    runner = _FakeRunner()
    now = time.monotonic()
    runner.rows = [
        BackgroundTask(
            run_id="run_a", name="alpha", status="running", started=now - 10
        ),
        BackgroundTask(
            run_id="run_b",
            name="beta",
            status="completed",
            started=now - 20,
            ended=now - 5,
        ),
    ]

    result = await ListAgentsTool(runner).execute({}, _ctx())

    assert result.success is True
    assert "- run_a alpha [running] 10s" in result.output
    assert "- run_b beta [completed] 15s" in result.output
    assert result.data is not None
    assert [row["runId"] for row in result.data["agents"]] == ["run_a", "run_b"]


@pytest.mark.asyncio
async def test_close_agent_reports_whether_the_run_was_live():
    runner = _FakeRunner()
    tool = CloseAgentTool(runner)

    runner.cancel_result = True
    live = await tool.execute({"agent_id": "run_a"}, _ctx())
    assert live.success is True
    assert live.output == "closed run_a"
    assert live.data == {"agent_id": "run_a", "closed": True}

    runner.cancel_result = False
    settled = await tool.execute({"agent_id": "run_b"}, _ctx())
    assert settled.success is True
    assert settled.output == "not running: run_b"
    assert settled.data == {"agent_id": "run_b", "closed": False}

    assert runner.cancels == ["run_a", "run_b"]


def _runner() -> SubAgentRunner:
    return SubAgentRunner(
        object(),
        engine_factory=lambda: None,
        build_tools=lambda _workdir: None,
    )


def test_runner_defaults_to_four_concurrent_children():
    assert _runner().max_concurrent == 4


@pytest.mark.asyncio
async def test_runner_wait_returns_unknown_for_an_id_it_never_saw():
    assert await _runner().wait("nope") == ("unknown", 0)


@pytest.mark.asyncio
async def test_runner_wait_reports_a_settled_row_without_a_task():
    runner = _runner()
    runner._background["run_done"] = BackgroundTask(
        run_id="run_done",
        name="done",
        status="completed",
        started=time.monotonic() - 0.2,
        ended=time.monotonic(),
    )

    status, elapsed_ms = await runner.wait("run_done", 1000)

    assert status == "completed"
    assert 100 <= elapsed_ms <= 400


@pytest.mark.asyncio
async def test_runner_wait_times_out_without_cancelling_the_child():
    runner = _runner()
    started = time.monotonic()
    row = BackgroundTask(run_id="run_live", name="live", status="running", started=started)
    runner._background["run_live"] = row
    child = asyncio.create_task(asyncio.sleep(0.3))
    runner._tasks["run_live"] = child

    status, elapsed_ms = await runner.wait("run_live", 50)

    assert status == "running"
    assert 40 <= elapsed_ms <= 400
    # The wait is an observation: the child must still be alive.
    assert not child.cancelled()
    assert not child.done()
    child.cancel()
    await asyncio.gather(child, return_exceptions=True)


@pytest.mark.asyncio
async def test_runner_wait_returns_the_status_once_the_task_settles():
    runner = _runner()
    started = time.monotonic()
    row = BackgroundTask(run_id="run_fast", name="fast", status="running", started=started)
    runner._background["run_fast"] = row

    async def settle() -> None:
        await asyncio.sleep(0.02)
        runner._background["run_fast"] = BackgroundTask(
            run_id="run_fast",
            name="fast",
            status="completed",
            started=started,
            ended=time.monotonic(),
        )

    runner._tasks["run_fast"] = asyncio.create_task(settle())

    status, elapsed_ms = await runner.wait("run_fast", 1000)

    assert status == "completed"
    assert elapsed_ms >= 0
