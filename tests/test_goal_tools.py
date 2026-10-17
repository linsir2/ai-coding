"""start_goal / update_goal (D-4): the model's own taskification surface.

The trust boundary these pin: a goal can start only from an ordinary turn --
not readonly (plan/planning), not while a task is already armed (which also
covers a goal round trying to start another goal), and a "blocked" claim is
refused before the third round of the episode (dsh `blockedAfterConsecutiveRounds`=3,
Codex "at least three consecutive goal turns").
"""

import pytest

from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext
from ai_coding.tools.goal_tools import StartGoalTool, UpdateGoalTool


def _ctx(task: TaskState, *, readonly: bool = False) -> ToolContext:
    return ToolContext(task=task, readonly=readonly)


@pytest.mark.asyncio
async def test_start_goal_arms_the_task_for_the_goal_profile():
    task = TaskState()
    result = await StartGoalTool().execute(
        {"objective": "把 jest 迁到 vitest"}, _ctx(task)
    )

    assert result.success is True
    assert task.goal == "把 jest 迁到 vitest"
    assert task.armed is True and task.armed_profile == "goal"
    assert task.goal_phase == ""
    # A fresh episode: an earlier episode's counters are not evidence about this one.
    assert task.rounds_run == 0 and task.resume_count == 0


@pytest.mark.asyncio
async def test_start_goal_keeps_the_existing_item_list():
    task = TaskState()
    task.add("先跑通 baseline")
    await StartGoalTool().execute({"objective": "g"}, _ctx(task))
    assert [item.content for item in task.items] == ["先跑通 baseline"]


@pytest.mark.asyncio
async def test_start_goal_refuses_a_second_goal_while_armed():
    task = TaskState(armed=True, armed_profile="goal")
    result = await StartGoalTool().execute({"objective": "another"}, _ctx(task))
    assert result.success is False
    assert result.error == "already_armed"
    assert task.goal == ""


@pytest.mark.asyncio
async def test_start_goal_refuses_in_readonly():
    task = TaskState()
    result = await StartGoalTool().execute(
        {"objective": "x"}, _ctx(task, readonly=True)
    )
    assert result.success is False and result.error == "readonly_session"
    assert task.armed is False


@pytest.mark.asyncio
async def test_start_goal_requires_an_objective():
    task = TaskState()
    result = await StartGoalTool().execute({"objective": "   "}, _ctx(task))
    assert result.success is False and result.error == "missing_objective"


@pytest.mark.asyncio
async def test_update_goal_complete_is_accepted_during_an_armed_goal():
    task = TaskState(goal="g", armed=True, armed_profile="goal")
    result = await UpdateGoalTool().execute({"status": "complete"}, _ctx(task))
    assert result.success is True
    assert task.goal_phase == "complete"


@pytest.mark.asyncio
async def test_update_goal_blocked_is_refused_before_three_rounds():
    task = TaskState(goal="g", armed=True, armed_profile="goal", rounds_run=2)
    result = await UpdateGoalTool().execute(
        {"status": "blocked", "note": "waiting on CI"}, _ctx(task)
    )
    assert result.success is False
    assert result.error == "block_threshold"
    assert task.goal_phase == ""


@pytest.mark.asyncio
async def test_update_goal_blocked_is_accepted_from_round_three():
    task = TaskState(goal="g", armed=True, armed_profile="goal", rounds_run=3)
    result = await UpdateGoalTool().execute(
        {"status": "blocked", "note": "same blocker three rounds"}, _ctx(task)
    )
    assert result.success is True
    assert task.goal_phase == "blocked"


@pytest.mark.asyncio
async def test_update_goal_blocked_requires_a_note():
    task = TaskState(goal="g", armed=True, armed_profile="goal", rounds_run=3)
    result = await UpdateGoalTool().execute({"status": "blocked"}, _ctx(task))
    assert result.success is False and result.error == "missing_note"


@pytest.mark.asyncio
async def test_update_goal_refuses_without_an_armed_goal():
    result = await UpdateGoalTool().execute({"status": "complete"}, _ctx(TaskState()))
    assert result.success is False and result.error == "no_goal"


@pytest.mark.asyncio
async def test_update_goal_refuses_on_a_plan_armed_task():
    task = TaskState(goal="g", armed=True, armed_profile="plan")
    result = await UpdateGoalTool().execute({"status": "complete"}, _ctx(task))
    assert result.success is False and result.error == "no_goal"
    assert task.goal_phase == ""
