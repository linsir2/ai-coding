"""TDD (M2 T3): the plan reminder is one thing, not a flag's side effect.

Today the plan posture is announced by two unrelated things: a static
``<plan-mode>`` section in the system prompt, and a one-shot planning
instruction in the transcript.  Neither survives a compaction, and neither
is withdrawn when the operator leaves plan mode -- so the model can read
"you are in plan mode" long after the operator left it, and can forget it
right after the reminder is compacted away.

The reminder is the third shape: a durable, synthetic message that is
queued when the agent changes, delivered at a turn boundary, and
**reconciled on every request** -- so a compaction that ate it puts it
back, and leaving plan mode says so explicitly.

The text and the decisions are pure functions here, so they can be
tested without a loop.
"""

from __future__ import annotations

import pytest

from ai_coding.core.plan_reminders import (
    PLAN_ENTER_REMINDER,
    PLAN_LEAVE_REMINDER,
    last_reminder,
    reconcile,
    reminder_for,
)
from ai_coding.domain.message import ChatMessage


def _user(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp="t")


def _assistant(content: str = "ok") -> ChatMessage:
    return ChatMessage(role="assistant", content=content, timestamp="t")


# ---- the two texts ----------------------------------------------------------


def test_the_two_reminders_are_defined_and_different():
    assert PLAN_ENTER_REMINDER.strip()
    assert PLAN_LEAVE_REMINDER.strip()
    assert PLAN_ENTER_REMINDER != PLAN_LEAVE_REMINDER


def test_the_reminders_are_marked_as_system_reminders():
    """The model must be able to tell them from what the human said."""
    assert PLAN_ENTER_REMINDER.startswith("<system-reminder>")
    assert PLAN_LEAVE_REMINDER.startswith("<system-reminder>")


def test_the_enter_reminder_says_a_conversational_yes_is_not_approval():
    """T12 (30 §3.4/M4b): the reminder's own copy — only present_plan's
    approval starts execution."""
    sentence = (
        "Only present_plan's approval starts execution; "
        "a conversational yes does not."
    )

    assert sentence in PLAN_ENTER_REMINDER
    assert PLAN_ENTER_REMINDER.count(sentence) == 1


def test_the_leave_reminder_says_the_restrictions_are_over():
    """An explicit withdrawal, not the absence of a new instruction."""
    assert "no longer" in PLAN_LEAVE_REMINDER.lower()


# ---- which reminder a switch produces ---------------------------------------


def test_entering_plan_produces_the_enter_reminder():
    assert reminder_for("build", "plan") == PLAN_ENTER_REMINDER


def test_leaving_plan_produces_the_leave_reminder():
    assert reminder_for("plan", "build") == PLAN_LEAVE_REMINDER


def test_switching_between_two_ordinary_agents_produces_nothing():
    """A new agent is not a new posture."""
    assert reminder_for("build", "reviewer") is None
    assert reminder_for("reviewer", "build") is None


def test_staying_in_plan_produces_nothing():
    assert reminder_for("plan", "plan") is None


# ---- finding the last reminder ----------------------------------------------


def test_the_last_reminder_is_found():
    messages = [
        _user("hi"),
        _user(PLAN_ENTER_REMINDER),
        _assistant("ok"),
    ]
    assert last_reminder(messages) == PLAN_ENTER_REMINDER


def test_a_history_without_a_reminder_has_none():
    assert last_reminder([_user("hi"), _assistant("ok")]) is None


def test_only_the_reminders_count():
    """A normal user message must not be mistaken for a reminder."""
    messages = [_user("you are in plan mode"), _assistant("ok")]
    assert last_reminder(messages) is None


def test_the_last_reminder_wins_over_an_earlier_one():
    messages = [
        _user(PLAN_ENTER_REMINDER),
        _assistant("ok"),
        _user(PLAN_LEAVE_REMINDER),
    ]
    assert last_reminder(messages) == PLAN_LEAVE_REMINDER


# ---- reconciling -------------------------------------------------------------


def test_a_plan_session_without_the_reminder_gets_one():
    """The compaction case: the reminder was eaten, so it is put back."""
    assert reconcile([_user("hi"), _assistant("ok")], is_plan=True) == PLAN_ENTER_REMINDER


def test_a_plan_session_that_has_it_gets_nothing():
    messages = [_user(PLAN_ENTER_REMINDER), _assistant("ok")]
    assert reconcile(messages, is_plan=True) is None


def test_a_build_session_without_a_reminder_gets_nothing():
    assert reconcile([_user("hi"), _assistant("ok")], is_plan=False) is None


def test_a_build_session_that_still_says_plan_gets_the_withdrawal():
    """Leaving plan mode says so; it does not merely stop saying otherwise."""
    messages = [_user(PLAN_ENTER_REMINDER), _assistant("ok")]
    assert reconcile(messages, is_plan=False) == PLAN_LEAVE_REMINDER


def test_a_build_session_that_already_left_gets_nothing():
    messages = [_user(PLAN_ENTER_REMINDER), _user(PLAN_LEAVE_REMINDER)]
    assert reconcile(messages, is_plan=False) is None


# ---- delivery and reconciliation in the real loop (M2 T3) -------------------


def _loop(tmp_path):
    from ai_coding.config.models import AIConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.loop import StepResult
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry

    class _Engine:
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return StepResult(text="done", finish_reason="stop")

        async def aclose(self):
            return None

    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        _Engine(),
        sessions,
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=2),
        workspace=str(tmp_path),
    )
    return loop, sessions


@pytest.mark.asyncio
async def test_the_reminder_is_delivered_at_the_turn_boundary(tmp_path):
    """The queued reminder becomes a synthetic message in the history."""
    loop, sessions = _loop(tmp_path)
    session = sessions.create()
    sessions.save(session)

    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
    assert session.pending_reminders  # queued, not delivered

    loop._apply_pending_reminders(session)

    assert session.pending_reminders is None
    delivered = [m for m in session.messages if m.content == PLAN_ENTER_REMINDER]
    assert len(delivered) == 1
    assert delivered[0].role == "user"
    assert delivered[0].synthetic is True


@pytest.mark.asyncio
async def test_delivery_survives_a_crash_between_the_two(tmp_path):
    """The posture landed, the reminder did not: it must still arrive."""
    loop, sessions = _loop(tmp_path)
    session = sessions.create()
    sessions.save(session)
    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")

    # The "crash": a new process over the same directory.
    loop2, sessions2 = _loop(tmp_path)
    reloaded = sessions2.load(session.session_id)
    assert reloaded.pending_reminders

    loop2._apply_pending_reminders(reloaded)
    assert reloaded.pending_reminders is None
    assert any(m.content == PLAN_ENTER_REMINDER for m in reloaded.messages)


def test_a_compacted_reminder_is_what_the_next_request_asks_for():
    """The history no longer says plan, but the session is in plan mode."""
    history = [_user("hi"), _assistant("ok")]
    assert reconcile(history, is_plan=True) == PLAN_ENTER_REMINDER


def test_the_reminder_is_not_the_humans_utterance():
    """A reminder is the harness talking; it must not pass for the human."""
    from ai_coding.core.request_assembly import human_utterance

    # The real shape: a delivered reminder is synthetic.
    history = [
        _user("the real question"),
        ChatMessage(
            role="user",
            content=PLAN_ENTER_REMINDER,
            timestamp="t",
            synthetic=True,
        ),
    ]
    assert human_utterance(history) == "the real question"


# ---- the planning instruction is the harness's too (M2 T3, Q4) --------------


@pytest.mark.asyncio
async def test_the_planning_instruction_is_synthetic(tmp_path):
    """The planning instruction is the harness's, so it is marked as one.

    It used to be told apart by its ``[plan] `` prefix -- a convention every
    reader had to learn.  Marking it is the same answer the reminder gets,
    and it lets the readers drop the prefix convention entirely.
    """
    from ai_coding.core.plan_runner import PLAN_MARK, plan_only

    loop, sessions = _loop(tmp_path)
    session = sessions.create()
    sessions.save(session)
    session.agent = "plan"
    session.readonly = True

    await plan_only(loop, session, topic="做一个东西")

    instructions = [
        m for m in session.messages if (m.content or "").startswith(PLAN_MARK)
    ]
    assert instructions, "the planning instruction was not sent"
    assert instructions[0].synthetic is True
