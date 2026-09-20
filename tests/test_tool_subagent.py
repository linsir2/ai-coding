"""TDD (M4-1): subAgent tool — delegate to a real SubAgentRunner.

The tool no longer holds an ``ai_service``; it delegates to an injected
:class:`SubAgentRunner` and forwards the optional worktree manager plus the
isolation flag.  Only the runner's conclusion (``SubagentResult.text``) is
returned to the caller as tool output.
"""

import pytest

from ai_coding.domain.subagent import SubagentResult
from ai_coding.tools.subagent_tool import SubAgentTool


class _FakeRunner:
    """Records how it is called and returns a canned conclusion."""

    def __init__(self, reply: str = "subagent result") -> None:
        self.reply = reply
        self.calls: list[tuple] = []

    async def run(self, task, name, *, worktree_manager=None, isolate=True):
        self.calls.append((task, name, worktree_manager, isolate))
        return SubagentResult(text=self.reply)


@pytest.mark.asyncio
async def test_subagent_delegates_to_runner_with_isolation_args():
    runner = _FakeRunner("done the task")
    tool = SubAgentTool(runner=runner, worktree_manager="wt/1", isolate=False)

    r = await tool.execute({"task": "analyze this code", "name": "analyzer"})

    assert r.success is True
    assert "done the task" in r.output
    assert runner.calls == [("analyze this code", "analyzer", "wt/1", False)]


@pytest.mark.asyncio
async def test_subagent_missing_task_fails():
    runner = _FakeRunner()
    tool = SubAgentTool(runner=runner)

    r = await tool.execute({})

    assert r.success is False
    assert "task" in r.output.lower()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_subagent_default_name_and_isolate_defaults():
    runner = _FakeRunner("ok")
    tool = SubAgentTool(runner=runner)  # wt=None, isolate defaults to True

    r = await tool.execute({"task": "do stuff"})

    assert r.success is True
    task, name, wt, isolate = runner.calls[0]
    assert name == "sub-agent"
    assert wt is None
    assert isolate is True


@pytest.mark.asyncio
async def test_subagent_empty_output_becomes_marker():
    runner = _FakeRunner("")
    tool = SubAgentTool(runner=runner)

    r = await tool.execute({"task": "t", "name": "n"})

    assert r.success is True
    assert "(sub-agent produced no output)" in r.output


@pytest.mark.asyncio
async def test_subagent_runner_error_is_tool_failure():
    class _BoomRunner:
        async def run(self, task, name, *, worktree_manager=None, isolate=True):
            raise RuntimeError("boom")

    tool = SubAgentTool(runner=_BoomRunner())

    r = await tool.execute({"task": "t"})

    assert r.success is False
    assert "boom" in r.output
