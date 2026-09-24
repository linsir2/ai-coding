"""TDD (R1a-5): subAgent tool — delegate with the parent's context, return a packet.

Rewritten from the M4 spec: the tool now hands the runner the parent turn's
request view and system prompt (so the sub-agent sees the same context), and it
turns the structured packet into a ToolResult with both readable text and
machine-readable data.
"""

import pytest

from ai_coding.domain.message import ChatMessage, ToolArtifact
from ai_coding.domain.subagent import SubagentResult
from ai_coding.tools.base import ToolContext
from ai_coding.tools.subagent_tool import SubAgentTool


class _FakeRunner:
    def __init__(self, result: SubagentResult | None = None) -> None:
        self.result = result or SubagentResult(status="completed", conclusion="done")
        self.calls: list[dict] = []

    async def run(
        self, task, name, *, worktree_manager=None, isolate=True, parent=None,
        parent_span="",
    ):
        self.calls.append(
            {
                "task": task,
                "name": name,
                "worktree_manager": worktree_manager,
                "isolate": isolate,
                "parent": parent,
            }
        )
        return self.result


def _ctx() -> ToolContext:
    return ToolContext(
        messages=(ChatMessage(role="user", content="parent view", timestamp="t"),),
        instructions="PARENT INSTRUCTIONS",
        workspace="/tmp/ws",
        session_id="sid",
    )


@pytest.mark.asyncio
async def test_subagent_tool_passes_parent_context():
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner, worktree_manager="wt/1", isolate=False)

    await tool.execute({"task": "analyze this", "name": "analyst"}, _ctx())

    call = runner.calls[0]
    assert call["task"] == "analyze this"
    assert call["name"] == "analyst"
    assert call["worktree_manager"] == "wt/1"
    assert call["isolate"] is False
    assert call["parent"] is not None
    assert call["parent"].instructions == "PARENT INSTRUCTIONS"
    assert [m.content for m in call["parent"].messages] == ["parent view"]


@pytest.mark.asyncio
async def test_subagent_tool_returns_structured_data():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="all good",
            todos=({"id": "1", "content": "step", "status": "pending"},),
            iterations=3,
            ledger_path="transcripts/subagents/abc.json",
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is True
    data = result.data
    assert data is not None
    assert data["status"] == "completed"
    assert data["iterations"] == 3
    assert data["todos"] == [{"id": "1", "content": "step", "status": "pending"}]
    assert data["ledger_path"] == "transcripts/subagents/abc.json"
    # the full conclusion travels as rendered text, not duplicated in data
    assert "conclusion" not in data
    assert data["conclusion_chars"] == len("all good")


@pytest.mark.asyncio
async def test_subagent_tool_renders_conclusion_and_open_items():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="I fixed the parser",
            todos=({"id": "2", "content": "write tests", "status": "pending"},),
            iterations=4,
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t", "name": "dev"}, _ctx())

    assert "I fixed the parser" in result.output
    assert "write tests" in result.output
    assert "dev" in result.output


@pytest.mark.asyncio
async def test_subagent_tool_passes_artifacts():
    runner = _FakeRunner(
        SubagentResult(
            status="completed",
            conclusion="done",
            artifacts=(ToolArtifact(path="parser.py", description="rewritten"),),
        )
    )
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert [a.path for a in result.artifacts] == ["parser.py"]
    assert "parser.py" in result.output


@pytest.mark.asyncio
async def test_subagent_missing_task_fails():
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({}, _ctx())

    assert result.success is False
    assert "task" in result.output.lower()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_subagent_timeout_is_failure():
    runner = _FakeRunner(SubagentResult(status="timeout", error="exceeded 600s"))
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is False
    assert result.data is not None
    assert result.data["status"] == "timeout"
    assert "timeout" in result.output.lower()


@pytest.mark.asyncio
async def test_subagent_runner_error_is_tool_failure():
    class _BoomRunner:
        async def run(
        self, task, name, *, worktree_manager=None, isolate=True, parent=None,
        parent_span="",
    ):
            raise RuntimeError("boom")

    tool = SubAgentTool(runner=_BoomRunner())

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is False
    assert result.error == "subagent_error"
    assert "boom" in result.output


@pytest.mark.asyncio
async def test_subagent_max_iterations_is_still_success():
    """A partial result beats discarding the work entirely."""
    runner = _FakeRunner(SubagentResult(status="max_iterations", conclusion="partial"))
    tool = SubAgentTool(runner=runner)

    result = await tool.execute({"task": "t"}, _ctx())

    assert result.success is True
    assert "partial" in result.output


def test_subagent_tool_declares_a_per_call_ceiling_above_the_runner_budget():
    """R2a: the dispatcher needs a ceiling to bound a stuck delegation."""

    class _BoundedRunner(_FakeRunner):
        timeout_seconds = 600.0

    assert SubAgentTool(runner=_BoundedRunner()).max_duration_seconds == pytest.approx(615.0)
    # A runner that does not advertise a budget leaves only the turn deadline.
    assert SubAgentTool(runner=_FakeRunner()).max_duration_seconds is None
