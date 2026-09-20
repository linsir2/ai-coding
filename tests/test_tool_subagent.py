"""TDD (M2-4): subAgent tool — skeleton for delegating to a sub-agent.

M2 keeps it simple: synchronous stub that echoes the task back with a marker.
M3 will add real model invocation and Worktree isolation.
"""

import pytest

from ai_coding.tools.subagent_tool import SubAgentTool


class _FakeAIService:
    def __init__(self, reply: str = "subagent result") -> None:
        self.reply = reply
        self.last_prompt: str | None = None

    async def execute_turn(self, history, user_input, token_sink=None):
        from ai_coding.domain.run import TurnResult
        self.last_prompt = user_input
        return TurnResult(text=self.reply)


@pytest.mark.asyncio
async def test_subagent_basic_call():

    ai = _FakeAIService("done the task")
    tool = SubAgentTool(ai_service=ai)
    r = await tool.execute({"task": "analyze this code", "name": "analyzer"})
    assert r.success is True
    assert "done the task" in r.output
    assert ai.last_prompt is not None
    assert "analyze this code" in ai.last_prompt


@pytest.mark.asyncio
async def test_subagent_missing_task_fails():

    ai = _FakeAIService("x")
    tool = SubAgentTool(ai_service=ai)
    r = await tool.execute({})
    assert r.success is False
    assert "task" in r.output.lower()


@pytest.mark.asyncio
async def test_subagent_default_name():

    ai = _FakeAIService("ok")
    tool = SubAgentTool(ai_service=ai)
    r = await tool.execute({"task": "do stuff"})
    assert r.success is True
