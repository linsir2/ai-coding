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

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))

    async def process_input(self, session, text, on_delta=None, observer=None):
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
    session.mode, session.readonly = "plan", True
    dc, c = _console()

    await handle_input(loop, "/goal 迁移 X", session, sessions, dc)
    assert "计划模式" in c.export_text()
    assert loop.calls == [] and sessions.saved == []

    session.mode, session.readonly = "normal", False
    session.task = TaskState(goal="g", armed=True, armed_profile="plan")
    dc2, c2 = _console()
    await handle_input(loop, "/goal 迁移 X", session, sessions, dc2)
    assert "先停止" in c2.export_text()
    assert loop.calls == [] and sessions.saved == []
    assert session.task.armed is True  # untouched
