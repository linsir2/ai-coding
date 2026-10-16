"""TDD (19 §3.2): present_plan — the blocking plan review, as a tool.

The review rides the existing ``UserPromptCallback`` channel (with ``detail``
and ``intent``), and the answer comes back as the tool result — the same shape
``ask_user`` already uses, for the same pairing reason (19 §2).  The review
card answers with a JSON string; plain words stay acceptable so the CLI path
keeps working.
"""

import pytest

from ai_coding.domain.task import TaskState
from ai_coding.security.user_prompt import UserAnswer
from ai_coding.tools.base import ToolContext
from ai_coding.tools.present_plan_tool import PresentPlanTool


class _Prompt:
    def __init__(self, answer: UserAnswer) -> None:
        self.answer = answer
        self.calls: list[tuple] = []

    async def ask(self, question, options=(), *, detail="", intent=""):
        self.calls.append((question, tuple(options), detail, intent))
        return self.answer


def _ctx(*, items: int = 1) -> ToolContext:
    task = TaskState(goal="g")
    for index in range(items):
        task.add(f"step {index + 1}")
    return ToolContext(task=task, workspace="/tmp/ws", session_id="sid")


def _tool(answer: UserAnswer) -> tuple[PresentPlanTool, _Prompt]:
    prompt = _Prompt(answer)
    return PresentPlanTool(prompt), prompt


@pytest.mark.asyncio
async def test_an_empty_plan_is_refused():
    tool, prompt = _tool(UserAnswer(answered=True, text="开始执行"))
    r = await tool.execute({"plan": "   "}, _ctx())
    assert r.success is False and r.error == "missing_plan"
    assert prompt.calls == []


@pytest.mark.asyncio
async def test_no_todo_items_is_refused_with_guidance():
    """19 §3.2: the plan must exist as todo items — the runner executes those."""
    tool, prompt = _tool(UserAnswer(answered=True, text="开始执行"))
    r = await tool.execute({"plan": "# 计划"}, _ctx(items=0))
    assert r.success is False and r.error == "no_plan_items"
    assert "todo_write" in r.output
    assert prompt.calls == []


@pytest.mark.asyncio
async def test_approve_rides_the_review_channel_and_reports_approved():
    tool, prompt = _tool(UserAnswer(answered=True, text='{"choice":"approve"}'))
    r = await tool.execute({"plan": "# 计划\n内容"}, _ctx())
    assert r.success is True
    assert r.data == {"approved": True}
    (question, options, detail, intent), = prompt.calls
    assert intent == "plan_review"
    assert options == ("开始执行", "继续规划")
    assert detail == "# 计划\n内容"
    assert "收尾" in r.output


@pytest.mark.asyncio
async def test_a_plain_approve_word_is_accepted():
    tool, _ = _tool(UserAnswer(answered=True, text="开始执行"))
    r = await tool.execute({"plan": "# p"}, _ctx())
    assert r.data == {"approved": True}


@pytest.mark.asyncio
async def test_keep_planning_carries_the_feedback_back():
    tool, _ = _tool(
        UserAnswer(answered=True, text='{"choice":"keep","feedback":"第 3 步拆细"}')
    )
    r = await tool.execute({"plan": "# p"}, _ctx())
    assert r.success is True
    assert r.data == {"approved": False}
    assert "第 3 步拆细" in r.output


@pytest.mark.asyncio
async def test_a_plain_text_reply_is_treated_as_feedback():
    tool, _ = _tool(UserAnswer(answered=True, text="把验收去掉"))
    r = await tool.execute({"plan": "# p"}, _ctx())
    assert r.data == {"approved": False}
    assert "把验收去掉" in r.output


@pytest.mark.asyncio
async def test_a_dismissed_review_tells_the_model_to_hold():
    tool, _ = _tool(UserAnswer(answered=False, reason="empty"))
    r = await tool.execute({"plan": "# p"}, _ctx())
    assert r.success is True
    assert r.data == {"approved": False, "dismissed": True}
    assert "保持计划模式" in r.output


@pytest.mark.asyncio
async def test_a_broken_channel_fails_without_breaking_the_turn():
    class _Boom:
        async def ask(self, question, options=(), *, detail="", intent=""):
            raise RuntimeError("boom")

    r = await PresentPlanTool(_Boom()).execute({"plan": "# p"}, _ctx())
    assert r.success is False and r.error == "user_prompt_error"


def test_it_is_a_sleeping_readonly_tool():
    """Same class discipline as ask_user: one human at a time, and the
    read-only plan gate must keep it callable (whitelist member)."""
    assert PresentPlanTool.parallel_safe is False
    assert PresentPlanTool.mutates_world is False
