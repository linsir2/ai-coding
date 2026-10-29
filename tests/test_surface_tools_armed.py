"""R21 §3: a plan round must not advertise the goal pair.

A plan run arms the task as ``plan`` (``plan_runner.py`` sets it before the
first step), so ``start_goal``/``update_goal`` can only answer ``no_goal``
there -- and did, which the transcript then drew as 未知原因（no_goal）.  The
fix is a surface predicate, not a prompt sentence: the request must not carry
a tool it cannot use.

The second half pins the cache key: the catalogue now depends on what the task
is armed as, so the same session must get a different list the moment that
fact changes.
"""

from ai_coding.domain.task import TaskState
from ai_coding.service.session_service import SessionService
from ai_coding.tools.goal_tools import StartGoalTool, UpdateGoalTool
from tests.test_agent_loop_react import ScriptedEngine, _loop, _registry, _text


def _names(schemas: list[dict]) -> list[str]:
    return [schema["function"]["name"] for schema in schemas]


def _loop_with_goals(tmp_path):
    sessions = SessionService(tmp_path / "sessions")
    # ``update_goal`` is asserted directly below; the registry needs both so a
    # "still offered" verdict means the predicate, not a missing tool.
    loop = _loop(
        ScriptedEngine([_text("unused")]),
        sessions,
        registry=_registry(StartGoalTool(), UpdateGoalTool()),
    )
    session = sessions.create()
    task = TaskState(goal="a plan")
    task.add("one")
    session.task = task
    return loop, session


def test_a_plan_round_does_not_offer_the_goal_pair(tmp_path):
    loop, session = _loop_with_goals(tmp_path)
    session.task.armed = True
    session.task.armed_profile = "plan"

    names = _names(loop._surface_schemas(session))

    assert "update_goal" not in names
    assert "start_goal" not in names


def test_a_goal_round_still_offers_them(tmp_path):
    loop, session = _loop_with_goals(tmp_path)
    session.task.armed = True
    session.task.armed_profile = "goal"

    names = _names(loop._surface_schemas(session))

    assert "update_goal" in names
    assert "start_goal" in names


def test_the_catalogue_changes_when_the_arm_changes(tmp_path):
    """The cache key carries ``armed_profile``; without it the first list wins."""
    loop, session = _loop_with_goals(tmp_path)
    session.task.armed = True
    session.task.armed_profile = "goal"
    assert "update_goal" in _names(loop._surface_schemas(session))

    session.task.armed_profile = "plan"

    assert "update_goal" not in _names(loop._surface_schemas(session))


def test_the_prompt_speaks_for_the_same_surface(tmp_path):
    """C-2: the methods section and the request are one rule, read twice."""
    loop, _session = _loop_with_goals(tmp_path)

    plan_armed = loop._prompt_tools("normal", False, (), (), "plan")
    goal_armed = loop._prompt_tools("normal", False, (), (), "goal")

    assert "update_goal" not in plan_armed
    assert "update_goal" in goal_armed


def test_an_unarmed_session_offers_them_as_before(tmp_path):
    """The predicate keys on ``armed``; a plain chat is untouched."""
    loop, session = _loop_with_goals(tmp_path)

    assert session.task.armed is False

    names = _names(loop._surface_schemas(session))
    assert "update_goal" in names and "start_goal" in names
