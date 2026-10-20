"""N1: ``finish_research`` — the second exit of a plan-mode turn.

A planning turn that answers a research question has nothing to execute, so
it must not be forced into a plan (the measured malformation: the work is
done first, then a plan is back-filled with invented verify commands).  This
tool is that other exit: the model states its conclusion, the turn ends, and
nothing is armed, executed or reviewed.

The two exits are mutually exclusive -- the first one to succeed owns the
turn -- so the tests below pin both refusals as well as the success.
"""

import pytest

from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext, TurnConclusion
from ai_coding.tools.finish_research_tool import FinishResearchTool


def _ctx(*, mode: str = "plan", conclusion: TurnConclusion | None = None) -> ToolContext:
    return ToolContext(
        task=TaskState(goal="g"),
        workspace="/tmp/ws",
        session_id="sid",
        mode=mode,
        conclusion=conclusion,
    )


@pytest.mark.asyncio
async def test_it_refuses_outside_plan_mode():
    """The exit belongs to the planning posture only.  A missing context fails
    closed the same way (the gate runs before anything else)."""
    tool = FinishResearchTool()

    r = await tool.execute({"summary": "结论"}, _ctx(mode="normal"))
    assert r.success is False and r.error == "not_in_plan"

    r2 = await tool.execute({"summary": "结论"}, None)
    assert r2.success is False and r2.error == "not_in_plan"


@pytest.mark.asyncio
async def test_it_refuses_an_empty_summary():
    tool = FinishResearchTool()

    r = await tool.execute({"summary": "   "}, _ctx())
    assert r.success is False and r.error == "missing_summary"


@pytest.mark.asyncio
async def test_it_closes_the_turn_with_the_conclusion_itself():
    """The summary is the deliverable: it is what the user reads, so it is the
    tool's output -- and nothing else happens.  No items, no arm, no card."""
    tool = FinishResearchTool()
    ctx = _ctx()

    r = await tool.execute({"summary": "记忆层是三层：working / episodic / semantic。"}, ctx)

    assert r.success is True
    assert r.output == "记忆层是三层：working / episodic / semantic。"
    assert r.data == {"research_finished": True}
    # Nothing was planned and nothing was armed: the runner must not wake up.
    assert ctx.task.items == []
    assert ctx.task.armed is False


@pytest.mark.asyncio
async def test_it_refuses_when_the_turn_already_concluded():
    """The exits are mutually exclusive: the first one to succeed owns the
    turn, so a research conclusion after a plan submission is refused."""
    tool = FinishResearchTool()
    conclusion = TurnConclusion(exit="present_plan")

    r = await tool.execute({"summary": "结论"}, _ctx(conclusion=conclusion))

    assert r.success is False and r.error == "already_concluded"
    assert "present_plan" in r.output


@pytest.mark.asyncio
async def test_it_refuses_while_a_plan_is_approved():
    """An approved plan owns the turn -- the runner is about to execute it, so
    a research conclusion would contradict the approval."""
    tool = FinishResearchTool()
    ctx = _ctx()
    ctx.task.armed = True
    ctx.task.armed_profile = "plan"

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is False and r.error == "plan_already_approved"


@pytest.mark.asyncio
async def test_it_refuses_while_a_goal_is_armed():
    """A running goal owns the task list; the research exit must not reach in
    and close a turn that is not its own."""
    tool = FinishResearchTool()
    ctx = _ctx()
    ctx.task.armed = True
    ctx.task.armed_profile = "goal"

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is False and r.error == "task_busy"


@pytest.mark.asyncio
async def test_a_successful_conclusion_marks_the_turn_concluded():
    """The holder is what the other exit reads: after this call, a plan
    submission in the same turn must be refused."""
    tool = FinishResearchTool()
    conclusion = TurnConclusion()
    ctx = _ctx(conclusion=conclusion)

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is True
    assert conclusion.exit == "finish_research"
