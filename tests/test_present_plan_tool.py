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


def _ctx(*, items: int = 1, mode: str = "plan") -> ToolContext:
    # R03: the tool is plan-only now; the tests that exercise its own checks
    # opt into the posture here (and the refusal tests opt out).
    task = TaskState(goal="g")
    for index in range(items):
        task.add(f"step {index + 1}")
    return ToolContext(
        task=task, workspace="/tmp/ws", session_id="sid", mode=mode
    )


def _tool(answer: UserAnswer) -> tuple[PresentPlanTool, _Prompt]:
    prompt = _Prompt(answer)
    return PresentPlanTool(prompt), prompt


def _plan_call(plan: str) -> dict:
    """A submission with one step, the shape the new contract requires."""
    return {"plan": plan, "items": [{"content": "做一件事", "verify": "true"}]}


@pytest.mark.asyncio
async def test_an_empty_plan_is_refused():
    tool, prompt = _tool(UserAnswer(answered=True, text="开始执行"))
    r = await tool.execute({"plan": "   "}, _ctx())
    assert r.success is False and r.error == "missing_plan"
    assert prompt.calls == []


@pytest.mark.asyncio
async def test_present_plan_refuses_outside_plan_mode():
    """R03: the tool is plan-only.  A normal/step/goal turn gets a structured
    refusal before anyone is asked; a missing context fails closed the same
    way (the gate runs before parsing or review)."""
    tool, prompt = _tool(UserAnswer(answered=True, text="开始执行"))
    r = await tool.execute(_plan_call("# p"), _ctx(mode="normal"))
    assert r.success is False and r.error == "not_in_plan"
    assert prompt.calls == []

    r2 = await tool.execute(_plan_call("# p"), None)
    assert r2.success is False and r2.error == "not_in_plan"
    assert prompt.calls == []


@pytest.mark.asyncio
async def test_present_plan_refuses_while_a_goal_is_armed():
    """R03: a non-plan task owns the list; approve must never be able to
    replace it.  The refusal fires before parsing and touches nothing."""
    tool, prompt = _tool(UserAnswer(answered=True, text='{"choice":"approve"}'))
    ctx = _ctx()
    assert ctx.task is not None
    ctx.task.armed = True
    ctx.task.armed_profile = "goal"

    r = await tool.execute(_plan_call("# p"), ctx)

    assert r.success is False and r.error == "task_busy"
    assert "goal" in r.output
    assert prompt.calls == []
    assert [item.content for item in ctx.task.items] == ["step 1"]
    assert ctx.task.armed is True and ctx.task.armed_profile == "goal"


@pytest.mark.asyncio
async def test_approve_rides_the_review_channel_and_reports_approved():
    tool, prompt = _tool(UserAnswer(answered=True, text='{"choice":"approve"}'))
    r = await tool.execute(_plan_call("# 计划\n内容"), _ctx())
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
    r = await tool.execute(_plan_call("# p"), _ctx())
    assert r.data == {"approved": True}


@pytest.mark.asyncio
async def test_keep_planning_carries_the_feedback_back():
    tool, _ = _tool(
        UserAnswer(answered=True, text='{"choice":"keep","feedback":"第 3 步拆细"}')
    )
    r = await tool.execute(_plan_call("# p"), _ctx())
    assert r.success is True
    assert r.data == {"approved": False}
    assert "第 3 步拆细" in r.output


@pytest.mark.asyncio
async def test_a_plain_text_reply_is_treated_as_feedback():
    tool, _ = _tool(UserAnswer(answered=True, text="把验收去掉"))
    r = await tool.execute(_plan_call("# p"), _ctx())
    assert r.data == {"approved": False}
    assert "把验收去掉" in r.output


@pytest.mark.asyncio
async def test_a_dismissed_review_tells_the_model_to_hold():
    tool, _ = _tool(UserAnswer(answered=False, reason="empty"))
    r = await tool.execute(_plan_call("# p"), _ctx())
    assert r.success is True
    assert r.data == {"approved": False, "dismissed": True}
    assert "保持计划模式" in r.output


@pytest.mark.asyncio
async def test_a_broken_channel_fails_without_breaking_the_turn():
    class _Boom:
        async def ask(self, question, options=(), *, detail="", intent=""):
            raise RuntimeError("boom")

    r = await PresentPlanTool(_Boom()).execute(_plan_call("# p"), _ctx())
    assert r.success is False and r.error == "user_prompt_error"


def test_it_is_a_sleeping_readonly_tool():
    """Same class discipline as ask_user: one human at a time, and the
    read-only plan gate must keep it callable (whitelist member)."""
    assert PresentPlanTool.parallel_safe is False
    assert PresentPlanTool.mutates_world is False


@pytest.mark.asyncio
async def test_approval_arms_the_task_for_boot_resume():
    """11-plan T4 (Q1=A): approval is the consent — the live task is armed
    right here, so the closing turn's own save carries it to disk and a
    crash before the run dispatch still auto-resumes."""
    tool, _ = _tool(UserAnswer(answered=True, text='{"choice":"approve"}'))
    ctx = _ctx()
    r = await tool.execute(_plan_call("# 计划"), ctx)
    assert r.success is True and r.data == {"approved": True}
    assert ctx.task is not None
    assert ctx.task.armed is True and ctx.task.armed_profile == "plan"


@pytest.mark.asyncio
async def test_keep_and_dismiss_leave_the_task_unarmed():
    tool, _ = _tool(UserAnswer(answered=True, text='{"choice":"keep","feedback":"改"}'))
    ctx = _ctx()
    await tool.execute(_plan_call("# 计划"), ctx)
    assert ctx.task.armed is False

    tool2, _ = _tool(UserAnswer(answered=False, text=""))
    ctx2 = _ctx()
    await tool2.execute(_plan_call("# 计划"), ctx2)
    assert ctx2.task.armed is False


# ---- P1: the steps ride this call, not the todo list -----------------------
#
# The measured failure (trace ec89d5ee): 8 steps written one ``todo_write add``
# at a time burned the whole iteration budget (every iteration asked for
# exactly one tool call), with an add -> clear -> add loop on top.  The steps
# now arrive in one structured submission, and approval is what turns them
# into the list the runner executes.


@pytest.mark.asyncio
async def test_items_are_required_in_the_call():
    """A plan with no steps is not executable, wherever they were written."""
    tool, prompt = _tool(UserAnswer(answered=True, text="开始执行"))
    r = await tool.execute({"plan": "# 计划"}, _ctx(items=0))
    assert r.success is False and r.error == "no_plan_items"
    assert "items" in r.output
    assert prompt.calls == []


@pytest.mark.asyncio
async def test_approved_items_land_in_the_task_with_their_verify():
    """Approval is the write point: the runner executes ``task.items``, and
    ``item.verify`` is what the acceptance gate runs against each step."""
    tool, _ = _tool(UserAnswer(answered=True, text='{"choice":"approve"}'))
    ctx = _ctx(items=0)
    r = await tool.execute(
        {
            "plan": "# 计划",
            "items": [
                {"content": "建目录", "verify": "test -d hotel"},
                {"content": "写 db.py"},
            ],
        },
        ctx,
    )
    assert r.success is True and r.data == {"approved": True}
    assert [item.content for item in ctx.task.items] == ["建目录", "写 db.py"]
    assert ctx.task.items[0].verify == "test -d hotel"
    assert ctx.task.items[1].verify == ""
    assert ctx.task.armed is True


@pytest.mark.asyncio
async def test_an_item_without_content_is_refused_before_the_human_sees_it():
    tool, prompt = _tool(UserAnswer(answered=True, text="开始执行"))
    r = await tool.execute(
        {"plan": "# p", "items": [{"content": "  ", "verify": "x"}]}, _ctx(items=0)
    )
    assert r.success is False and r.error == "invalid_items"
    assert prompt.calls == []


@pytest.mark.asyncio
async def test_keep_planning_does_not_write_the_items():
    """Only approval writes.  Keep-planning hands the feedback back for a
    revision, so a rejected draft must leave no trace in the list."""
    tool, _ = _tool(
        UserAnswer(answered=True, text='{"choice":"keep","feedback":"第 3 步拆细"}')
    )
    ctx = _ctx(items=0)
    r = await tool.execute(
        {"plan": "# p", "items": [{"content": "建目录"}]}, ctx
    )
    assert r.data == {"approved": False}
    assert ctx.task.items == []


@pytest.mark.asyncio
async def test_a_dismissed_review_does_not_write_the_items():
    tool, _ = _tool(UserAnswer(answered=False, reason="empty"))
    ctx = _ctx(items=0)
    await tool.execute({"plan": "# p", "items": [{"content": "建目录"}]}, ctx)
    assert ctx.task.items == []


@pytest.mark.asyncio
async def test_approval_replaces_the_list_with_the_submitted_items():
    """dsh's shape (``tool-todo/src/index.ts:80`` replaces the whole table):
    the submission IS the list.

    Appending would let a model that pre-wrote items with ``todo_write``
    (against the prompt) leave orphan steps behind, and the runner would
    execute them too — measured in a real loop run: three items where the
    user had approved two.
    """
    tool, _ = _tool(UserAnswer(answered=True, text='{"choice":"approve"}'))
    ctx = _ctx(items=2)  # items the model wrote before submitting
    r = await tool.execute({"plan": "# p", "items": [{"content": "新步骤"}]}, ctx)

    assert r.data == {"approved": True}
    assert [item.content for item in ctx.task.items] == ["新步骤"]
    assert ctx.task.items[0].id == "1"  # ids restart: this is a new list
    assert ctx.task.goal == "g"  # the objective is the episode's, not the list's
