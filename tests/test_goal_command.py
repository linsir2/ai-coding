"""Goal command routing (D-4): /goal <objective> / /goal off / bare /goal.

The human entry is the spelling of ``start_goal``: same episode (one place:
``TaskState.arm_goal``), same runner, same disarm seam (esc / off).  The run
blocks in the command like ``/plan run`` does, so one command is one envelope.
"""

import pytest
from rich.console import Console

from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.console import DialogueConsole
from ai_coding.app.slash import parse_slash
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState


class _Loop:
    """The stub the plan-command tests use: policy + recorded turns."""

    def __init__(self, *, settings: object = None) -> None:
        self.calls: list[str] = []
        self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))
        if settings is not None:
            self.settings = settings

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        self.calls.append(text)
        return "done"


class _Sessions:
    def __init__(self) -> None:
        self.saved: list = []

    def save(self, session) -> None:
        self.saved.append(session)


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


def _console() -> tuple[DialogueConsole, Console]:
    c = Console(record=True, width=80)
    return DialogueConsole(c), c


def test_goal_is_a_known_slash_command():
    cmd = parse_slash("/goal 把 jest 迁到 vitest")
    assert cmd is not None and cmd.name == "goal"


@pytest.mark.asyncio
async def test_goal_objective_arms_saves_and_drives_the_runner():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    action, _ = await handle_input(loop, "/goal 把 jest 迁到 vitest", session, sessions, dc)

    assert action is REPLAction.CONTINUE
    assert sessions.saved == [session]  # armed is durable before any round
    assert loop.calls and loop.calls[0].startswith("[goal] 第 1/20 轮")
    assert "把 jest 迁到 vitest" in loop.calls[0]
    # The stub turn makes no tool call, so the no-progress guard ends the run
    # after one round and every exit disarms.
    assert session.task.armed is False
    text = c.export_text()
    assert "已立案" in text
    assert "[goal] 共 1 轮" in text and "无进展" in text


@pytest.mark.asyncio
async def test_goal_off_disarms_and_saves():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=True, armed_profile="goal")
    dc, c = _console()

    await handle_input(loop, "/goal off", session, sessions, dc)

    assert session.task.armed is False
    assert sessions.saved == [session]
    assert loop.calls == []
    assert "已停止" in c.export_text()


@pytest.mark.asyncio
async def test_goal_off_without_a_run_is_a_notice_only():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/goal off", session, sessions, dc)

    assert sessions.saved == [] and loop.calls == []
    assert "没有在跑的任务" in c.export_text()


@pytest.mark.asyncio
async def test_bare_goal_reports_status():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/goal", session, sessions, dc)
    assert "未立案" in c.export_text()

    session.task = TaskState(goal="迁移 X", armed=True, armed_profile="goal", rounds_run=3)
    dc2, c2 = _console()
    await handle_input(loop, "/goal", session, sessions, dc2)
    text = c2.export_text()
    assert "迁移 X" in text and "rounds=3" in text
    assert loop.calls == []


@pytest.mark.asyncio
async def test_goal_refuses_in_plan_mode_and_while_another_task_is_armed():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.agent, session.readonly = "plan", True
    dc, c = _console()

    await handle_input(loop, "/goal 迁移 X", session, sessions, dc)
    assert "计划模式" in c.export_text()
    assert loop.calls == [] and sessions.saved == []

    session.agent, session.readonly = "build", False
    session.task = TaskState(goal="g", armed=True, armed_profile="plan")
    dc2, c2 = _console()
    await handle_input(loop, "/goal 迁移 X", session, sessions, dc2)
    assert "先停止" in c2.export_text()
    assert loop.calls == [] and sessions.saved == []
    assert session.task.armed is True  # untouched


# ---- G2: /goal resume continues the same episode (2026-10-17) ---------------


@pytest.mark.asyncio
async def test_goal_resume_rearms_the_same_episode_without_resetting_counters():
    from types import SimpleNamespace

    loop = _Loop(settings=SimpleNamespace(goal_max_rounds=20, goal_token_budget=0))
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(
        goal="迁移 X", armed=False, armed_profile="goal",
        rounds_run=3, tokens_used=500,
    )
    dc, c = _console()

    await handle_input(loop, "/goal resume", session, sessions, dc)

    assert loop.calls and loop.calls[0].startswith("[goal] 第 4/20 轮"), (
        "resume continues the episode's round numbering"
    )
    assert session.task.rounds_run == 4, "run from 3, not from 0"
    assert session.task.tokens_used == 500, "the episode's account is kept"
    assert session.task.armed is False  # the run stopped (no-progress) and disarmed


@pytest.mark.asyncio
async def test_goal_resume_is_refused_without_a_goal():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/goal resume", session, sessions, dc)

    assert loop.calls == [] and sessions.saved == []
    assert "没有可续跑的目标" in c.export_text()


@pytest.mark.asyncio
async def test_goal_resume_is_refused_while_armed():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=True, armed_profile="goal")
    dc, c = _console()

    await handle_input(loop, "/goal resume", session, sessions, dc)

    assert loop.calls == []
    assert "已在跑" in c.export_text()


@pytest.mark.asyncio
async def test_goal_resume_is_refused_in_plan_mode():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.agent, session.readonly = "plan", True
    session.task = TaskState(goal="迁移 X", armed=False)
    dc, c = _console()

    await handle_input(loop, "/goal resume", session, sessions, dc)

    assert loop.calls == []
    assert "计划模式" in c.export_text()


@pytest.mark.asyncio
async def test_goal_resume_is_refused_after_completion():
    """dsh parity: a completed goal is terminal -- resume must not reopen it."""
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=False, armed_profile="goal",
                             goal_phase="complete", rounds_run=2)
    dc, c = _console()

    await handle_input(loop, "/goal resume", session, sessions, dc)

    assert loop.calls == []
    assert session.task.goal_phase == "complete", "the terminal claim is untouched"
    assert "终态" in c.export_text()


@pytest.mark.asyncio
async def test_goal_resume_clears_a_blocked_phase_so_the_first_round_runs():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=False, armed_profile="goal",
                             goal_phase="blocked", rounds_run=4)
    dc, c = _console()

    await handle_input(loop, "/goal resume", session, sessions, dc)

    assert session.task.goal_phase == "", "a stale claim must not stop round one"
    assert loop.calls, "the resumed run actually ran"


@pytest.mark.asyncio
async def test_goal_resume_warns_when_the_budgets_are_already_spent():
    from types import SimpleNamespace

    loop = _Loop(settings=SimpleNamespace(goal_max_rounds=20, goal_token_budget=100))
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=False, armed_profile="goal",
                             rounds_run=1, tokens_used=100)
    dc, c = _console()

    await handle_input(loop, "/goal resume", session, sessions, dc)

    text = c.export_text()
    assert "已用尽" in text, "the user is told the run will stop again immediately"
    assert loop.calls, "the warning does not refuse -- the run still happens"


@pytest.mark.asyncio
async def test_goal_pause_redirects_to_off_and_never_arms():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    dc, c = _console()

    await handle_input(loop, "/goal pause", session, sessions, dc)

    text = c.export_text()
    assert "/goal off" in text
    assert session.task is None or not session.task.goal, (
        "a control word must not become an objective"
    )
    assert loop.calls == []


@pytest.mark.asyncio
async def test_goal_off_notice_points_at_resume():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=True, armed_profile="goal")
    dc, c = _console()

    await handle_input(loop, "/goal off", session, sessions, dc)

    assert "/goal resume" in c.export_text()


@pytest.mark.asyncio
async def test_bare_goal_status_shows_the_token_account():
    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=False, armed_profile="goal",
                             rounds_run=3, tokens_used=24_077)
    dc, c = _console()

    await handle_input(loop, "/goal", session, sessions, dc)

    assert "tokens=24077" in c.export_text()


@pytest.mark.asyncio
async def test_goal_off_emits_a_task_updated_so_an_idle_stop_is_seen():
    """G2: the off path tells the wire too -- the goal line must leave when
    the user stops an idle goal, without waiting for another run's arm."""
    from ai_coding.core.events import RecordingObserver, TaskUpdated

    loop = _Loop()
    sessions = _Sessions()
    session = _session()
    session.task = TaskState(goal="迁移 X", armed=True, armed_profile="goal")
    dc, _c = _console()
    observer = RecordingObserver()

    await handle_input(loop, "/goal off", session, sessions, dc, observer=observer)

    events = observer.of_type(TaskUpdated)
    assert len(events) == 1 and events[0].task.armed is False
