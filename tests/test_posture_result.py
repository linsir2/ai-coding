"""TDD (M1b T10): the four answers a posture request can have.

The old API said "landed or not".  These four say which of the four things
actually happened, because "not now" and "already queued" and "you just
cancelled it" are three different things to tell an operator.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.agents.registry import builtin_catalog
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import AgentSwitched
from ai_coding.domain.loop import StepResult
from ai_coding.domain.stance import PostureResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


def test_the_four_states_are_distinct_and_named():
    assert [state.value for state in PostureResult] == [
        "committed",
        "queued",
        "cancelled",
        "noop",
    ]


def test_a_state_is_compared_with_is_not_by_truthiness():
    """Every member is truthy, so ``if not result`` would take the wrong branch."""
    assert all(bool(state) for state in PostureResult)
    assert PostureResult.COMMITTED is not PostureResult.QUEUED


# ---- the engine -------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


class _TextEngine:
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="done", finish_reason="stop")

    async def aclose(self) -> None:
        return None


class _BusyLoop(AgentLoop):
    """A loop whose turn is running — the boundary the queue exists for.

    ``turn_running`` is the one fact a posture request reads; holding it true
    keeps the test about the queue rather than about how a turn is started.
    """

    @property
    def turn_running(self) -> bool:
        return True


def _loop(tmp_path, observer=None, *, busy: bool = False) -> tuple[AgentLoop, SessionService]:
    sessions = SessionService(tmp_path / "sessions")
    loop = (_BusyLoop if busy else AgentLoop)(
        _TextEngine(),
        sessions,
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=2),
        workspace=str(tmp_path),
        agents=builtin_catalog(),
        observer=observer,
    )
    return loop, sessions


def _session(sessions: SessionService):
    return sessions.create()


# ---- the four answers -------------------------------------------------------


def test_idle_and_different_commits(tmp_path):
    loop, sessions = _loop(tmp_path)
    session = _session(sessions)
    assert loop.request_posture(session, agent="plan", readonly=True) is PostureResult.COMMITTED
    assert (session.agent, session.readonly) == ("plan", True)


def test_idle_and_same_is_a_noop(tmp_path):
    loop, sessions = _loop(tmp_path)
    session = _session(sessions)
    assert loop.request_posture(session, agent="build", readonly=False) is PostureResult.NOOP
    assert session.pending_posture is None


def test_a_queued_flip_is_written_to_a_sidecar(tmp_path):
    loop, sessions = _loop(tmp_path, busy=True)
    session = _session(sessions)
    sessions.save(session)
    result = loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
    assert result is PostureResult.QUEUED
    sidecar = tmp_path / "sessions" / "pending-posture" / f"{session.session_id}.json"
    assert sidecar.is_file()
    assert (session.agent, session.readonly) == ("build", False)


def test_asking_for_the_current_state_withdraws_a_queued_flip(tmp_path):
    loop, sessions = _loop(tmp_path, busy=True)
    session = _session(sessions)
    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
    result = loop.request_posture(session, agent="build", readonly=False, reason="/plan off")
    assert result is PostureResult.CANCELLED
    assert session.pending_posture is None


def test_queueing_the_same_target_twice_is_still_queued(tmp_path):
    loop, sessions = _loop(tmp_path, busy=True)
    session = _session(sessions)
    first = loop.request_posture(session, agent="plan", readonly=True)
    before = dict(session.pending_posture)
    second = loop.request_posture(session, agent="plan", readonly=True)
    assert first is PostureResult.QUEUED
    assert second is PostureResult.QUEUED
    assert session.pending_posture == before


def test_committing_records_history_and_emits_the_event(tmp_path):
    recorder = _Recorder()
    loop, sessions = _loop(tmp_path, observer=recorder)
    session = _session(sessions)

    loop.request_posture(
        session, agent="plan", readonly=True, reason="/plan", observer=recorder
    )

    assert session.agent_history == [
        {
            "from": "build",
            "to": "plan",
            "ts": session.agent_history[0]["ts"],
            "reason": "/plan",
        }
    ]
    switched = [event for event in recorder.events if isinstance(event, AgentSwitched)]
    assert len(switched) == 1
    assert (switched[0].from_agent, switched[0].to_agent) == ("build", "plan")


def test_a_readonly_only_change_records_no_history(tmp_path):
    loop, sessions = _loop(tmp_path)
    session = _session(sessions)
    loop.request_posture(session, readonly=True)
    assert session.readonly is True
    assert session.agent_history == []


def test_a_readonly_only_change_still_announces_the_posture(tmp_path):
    """R21 §2: ``/readonly`` moved this axis and emitted nothing at all.

    The front end derives both posture axes from its own state, so a flip with
    no event left the status bar showing the old one until some unrelated
    refresh happened to land.
    """
    recorder = _Recorder()
    loop, sessions = _loop(tmp_path, recorder)
    session = _session(sessions)

    loop.request_posture(session, readonly=True)

    switched = [event for event in recorder.events if isinstance(event, AgentSwitched)]
    assert len(switched) == 1
    assert switched[0].readonly is True
    # Same agent on both ends: the notice is suppressed, not invented.
    assert (switched[0].from_agent, switched[0].to_agent) == ("build", "build")


def test_the_switch_event_names_the_read_only_axis(tmp_path):
    recorder = _Recorder()
    loop, sessions = _loop(tmp_path, recorder)
    session = _session(sessions)

    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")

    switched = [event for event in recorder.events if isinstance(event, AgentSwitched)]
    assert len(switched) == 1
    assert switched[0].readonly is True
    # And the history row keeps its old shape (no readonly key to replay).
    assert set(session.agent_history[0]) == {"from", "to", "ts", "reason"}


def test_the_queued_flip_lands_at_the_turn_boundary(tmp_path):
    loop, sessions = _loop(tmp_path, busy=True)
    session = _session(sessions)
    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
    loop._apply_pending_posture(session)
    assert (session.agent, session.readonly) == ("plan", True)
    assert session.pending_posture is None
    assert session.agent_history[-1]["reason"] == "/plan"


def test_a_queued_flip_survives_a_crash(tmp_path):
    """The sidecar is what the in-memory copy cannot be: durable."""
    loop, sessions = _loop(tmp_path)
    session = _session(sessions)
    sessions.save(session)
    sessions.mark_pending_posture(
        session.session_id,
        {"agent": "plan", "readonly": True, "reason": "/plan"},
    )
    loaded = sessions.load(session.session_id)
    assert loaded.pending_posture == {
        "agent": "plan",
        "readonly": True,
        "reason": "/plan",
    }
    loop._apply_pending_posture(loaded)
    assert loaded.agent == "plan"
    assert not (
        tmp_path / "sessions" / "pending-posture" / f"{session.session_id}.json"
    ).exists()


def test_deleting_a_session_clears_its_posture_marker(tmp_path):
    _, sessions = _loop(tmp_path)
    session = _session(sessions)
    sessions.save(session)
    sessions.mark_pending_posture(session.session_id, {"agent": "plan"})
    assert sessions.delete(session.session_id) is True
    assert not (
        tmp_path / "sessions" / "pending-posture" / f"{session.session_id}.json"
    ).exists()


@pytest.mark.asyncio
async def test_a_sidecar_failure_does_not_end_the_turn(tmp_path):
    """Durability is best-effort; the in-memory queue is the truth in-process."""
    loop, sessions = _loop(tmp_path, busy=True)
    session = _session(sessions)
    sessions.save(session)
    # The marker's parent cannot be a directory, so the atomic write fails
    # with an OSError — the shape a read-only root produces in production.
    (tmp_path / "sessions" / "pending-posture").write_text("", encoding="utf-8")
    result = loop.request_posture(session, agent="plan", readonly=True)
    assert result is PostureResult.QUEUED
    assert session.pending_posture is not None
    await loop.aclose()


# ---- the reminder's durable queue (M2 T3) -----------------------------------


def test_a_switch_queues_a_reminder(tmp_path):
    """The reminder is queued when the agent changes, and it is durable.

    "The posture landed but the reminder has not been delivered" is a
    real state -- the process can die between the two -- and a reminder
    that only lives in memory is lost exactly there.
    """
    from ai_coding.core.plan_reminders import PLAN_ENTER_REMINDER

    loop, sessions = _loop(tmp_path)
    session = _session(sessions)
    sessions.save(session)

    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")

    assert session.pending_reminders == [
        {"kind": "enter", "text": PLAN_ENTER_REMINDER}
    ]
    sidecar = tmp_path / "sessions" / "pending-reminder" / f"{session.session_id}.json"
    assert sidecar.is_file()


def test_leaving_plan_queues_the_withdrawal(tmp_path):
    from ai_coding.core.plan_reminders import PLAN_LEAVE_REMINDER

    loop, sessions = _loop(tmp_path)
    session = _session(sessions)
    session.agent = "plan"
    sessions.save(session)

    loop.request_posture(session, agent="build", readonly=False, reason="/plan off")

    assert session.pending_reminders == [
        {"kind": "leave", "text": PLAN_LEAVE_REMINDER}
    ]


def test_switching_between_ordinary_agents_queues_nothing(tmp_path):
    loop, sessions = _loop(tmp_path)
    session = _session(sessions)

    loop.request_posture(session, agent="reviewer", readonly=False)

    assert session.pending_reminders is None


def test_the_queued_reminder_comes_back_after_a_crash(tmp_path):
    loop, sessions = _loop(tmp_path)
    session = _session(sessions)
    sessions.save(session)
    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")

    reloaded = sessions.load(session.session_id)
    assert reloaded.pending_reminders
    assert reloaded.pending_reminders[0]["kind"] == "enter"
