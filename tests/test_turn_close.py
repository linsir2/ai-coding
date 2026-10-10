"""R7a: what a turn leaves behind — the closing half, in one unit.

The loop's tail used to be five private methods: ``_write_tool_messages``,
``_checkpoint``, ``_finalize``, ``_save`` and ``_close_turn``, plus four
module-level factories that turned "stop, but why" into a ``TurnOutcome``.  They
all answer one question — *what does this turn leave on disk and in the UI?* — so
they move together (docs/r7-analysis.md 3.1).
"""

from __future__ import annotations

from typing import Any

from ai_coding.core.turn_close import (
    TurnCloser,
    aborted_outcome,
    failed_outcome,
    stopped_outcome,
    terminal_outcome,
)
from ai_coding.core.turn_policy import TurnAction, TurnDecision
from ai_coding.domain.loop import EmptyStepError, TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolArtifact
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.infra.deadline import DeadlineExpired


class _Sessions:
    """A session store that remembers only whether it was asked to save."""

    def __init__(self, fail: bool = False) -> None:
        self.saved: list[int] = []
        self.fail = fail

    def save(self, session: SessionData) -> None:
        if self.fail:
            raise OSError("disk on fire")
        self.saved.append(len(session.messages))


class _Reporter:
    """Stands in for TurnReporter: records the finish, keeps the damage report."""

    def __init__(self) -> None:
        self.finished: list[TurnOutcome] = []
        self.degraded: list[tuple[str, str, str]] = []

    def finish_turn(self, outcome: TurnOutcome, observer: Any) -> TurnOutcome:
        self.finished.append(outcome)
        return outcome

    def report_degraded(self, component: str, reason: str, fallback: str = "") -> None:
        self.degraded.append((component, reason, fallback))


class _Settings:
    persist_checkpoint_messages = 100
    working_memory_keep = 12


def _closer(*, fail_save: bool = False) -> tuple[TurnCloser, SessionData, _Reporter, _Sessions]:
    reporter = _Reporter()
    sessions = _Sessions(fail=fail_save)
    session = SessionData(session_id="s1", title="", created_time="t", last_access_time="t")
    closer = TurnCloser(sessions, reporter, _Settings())
    return closer, session, reporter, sessions


# ------------------------------------------------------------ the factories


def test_terminal_outcome_maps_the_turn_clock_to_a_timeout():
    outcome = terminal_outcome(
        TurnDecision(TurnAction.FINISH, "turn deadline exceeded"),
        budget_expired=True,
        last_text=None,
        iterations=2,
    )
    assert outcome.status is TurnStatus.TIMEOUT
    assert outcome.reason == "turn deadline exceeded"
    assert outcome.iterations == 2


def test_terminal_outcome_maps_the_token_budget():
    outcome = terminal_outcome(
        TurnDecision(TurnAction.FINISH, "turn token budget exhausted"),
        budget_expired=False,
        last_text="半句",
        iterations=3,
    )
    assert outcome.status is TurnStatus.BUDGET
    assert outcome.text == "半句"  # partial work is kept


def test_terminal_outcome_defaults_to_max_iterations():
    outcome = terminal_outcome(
        TurnDecision(TurnAction.FINISH, "iteration budget exhausted"),
        budget_expired=False,
        last_text=None,
        iterations=10,
    )
    assert outcome.status is TurnStatus.MAX_ITERATIONS


def test_failed_outcome_names_the_label_that_timed_out():
    outcome = failed_outcome(
        DeadlineExpired("model_step", 30.0), last_text="半句", iterations=4
    )
    assert outcome.status is TurnStatus.TIMEOUT
    assert "model_step" in (outcome.reason or "")
    assert outcome.text == "半句"


def test_failed_outcome_maps_an_engine_error_to_no_text():
    outcome = failed_outcome(RuntimeError("boom"), last_text="半句", iterations=1)
    assert outcome.status is TurnStatus.ERROR
    assert outcome.text is None  # a failed call is not the model's last word
    assert outcome.reason == "boom"


def test_failed_outcome_names_an_empty_step_in_words_and_keeps_the_finish_reason():
    """A6 T4: the provider returned thinking and nothing else.  Same shape as
    the deadline branch above — a reason a person can act on, not the class name
    and the Python message.  finish_reason rides along because it tells the
    operator whether this was an upstream stop or our own doing."""
    outcome = failed_outcome(
        EmptyStepError("stop"), last_text=None, iterations=1
    )
    assert outcome.status is TurnStatus.ERROR
    assert outcome.text is None
    assert "没有回答" in (outcome.reason or "")
    assert "stop" in (outcome.reason or "")


def test_stopped_and_aborted_keep_the_partial_work():
    assert stopped_outcome("半句", 2).status is TurnStatus.STOPPED
    assert aborted_outcome("半句", 2).status is TurnStatus.ABORTED
    assert stopped_outcome("半句", 2).text == "半句"
    assert aborted_outcome("半句", 2).text == "半句"


# ------------------------------------------------------------- the writing


def test_write_back_appends_a_tool_message_and_collects_artifacts():
    from ai_coding.core.dispatch import DispatchOutcome
    from ai_coding.domain.message import ToolCall, ToolResult

    closer, session, _, _ = _closer()
    closer.write_back(
        session,
        [
            DispatchOutcome(
                call=ToolCall(id="c1", name="bash"),
                result=ToolResult(
                    success=True,
                    output="ok",
                    artifacts=(ToolArtifact(path="a.py", kind="write"),),
                ),
            )
        ],
    )
    assert [message.role for message in session.messages] == ["tool"]
    assert session.messages[0].tool_call_id == "c1"
    assert [artifact.path for artifact in closer.artifacts] == ["a.py"]


def test_checkpoint_is_throttled_on_a_long_session():
    closer, session, _, sessions = _closer()
    closer.checkpoint(session, index=1)  # small session → saved
    assert sessions.saved == [0]
    for _ in range(101):  # one past the persist limit (100)
        session.messages.append(_filler())
    sessions.saved.clear()
    closer.checkpoint(session, index=1)  # long session, index 1 → skipped
    assert sessions.saved == []
    closer.checkpoint(session, index=3)  # the stride lands → saved
    assert sessions.saved == [101]


def test_a_failed_save_is_reported_not_raised():
    closer, session, reporter, _ = _closer(fail_save=True)
    closer.save(session)
    assert reporter.degraded
    assert reporter.degraded[0][0] == "persist"


def test_close_adds_a_handoff_only_when_the_model_said_nothing():
    closer, session, reporter, _ = _closer()
    silent = closer.close(
        session,
        user_input="修好它",
        task=TaskState(goal="修好它"),
        turn_number=1,
        outcome=TurnOutcome(
            status=TurnStatus.TIMEOUT, text=None, iterations=2, reason="turn deadline exceeded"
        ),
        failures=[("bash", "tool_timeout", "")],
    )
    assert silent.text is None
    assert silent.handoff.startswith("[回合未完成")
    assert "bash" in silent.handoff
    assert reporter.finished[-1] is silent


def test_close_never_rewrites_the_text_it_was_given():
    closer, session, _, _ = _closer()
    spoken = closer.close(
        session,
        user_input="做完了吗",
        task=TaskState(goal="做完了吗"),
        turn_number=1,
        outcome=TurnOutcome(
            status=TurnStatus.COMPLETED, text="做完了", iterations=1, reason="model finished"
        ),
    )
    assert spoken.text == "做完了"
    assert spoken.handoff == ""


def test_close_appends_one_digest_per_turn():
    closer, session, _, _ = _closer()
    for number in (1, 2):
        closer.close(
            session,
            user_input=f"第 {number} 步",
            task=TaskState(goal="长任务"),
            turn_number=number,
            outcome=TurnOutcome(
                status=TurnStatus.COMPLETED, text="好", iterations=1, reason="model finished"
            ),
        )
    assert [digest.turn for digest in session.turn_digests] == [1, 2]


def _filler():
    from ai_coding.domain.message import ChatMessage

    return ChatMessage(role="user", content="x", timestamp="")


# ---- G8-2: the model gets the last word, without losing the banner ----------


def test_a_model_closing_is_shown_inside_the_unfinished_report():
    """The turn genuinely ran out of iterations, so the banner stays: it is the
    harness's promise that this turn did not finish.  The model's closing words
    are added to it rather than replacing it -- letting prose silently stand in
    for the banner would let a cut-off turn read as a completed one.
    """
    from ai_coding.core.turn_close import terminal_outcome
    from ai_coding.domain.loop import TurnStatus
    from ai_coding.domain.turn_digest import render_handoff

    outcome = terminal_outcome(
        TurnDecision(TurnAction.FINISH, "iteration budget exhausted"),
        budget_expired=False,
        last_text=None,
        iterations=10,
    )
    text = render_handoff(outcome=outcome, closing="读完了三个文件，还差一个用例没写。")

    assert "[回合未完成" in text
    assert "读完了三个文件，还差一个用例没写。" in text
    assert text.index("[回合未完成") < text.index("读完了三个文件")
    assert outcome.status is TurnStatus.MAX_ITERATIONS


def test_without_a_closing_the_report_is_unchanged():
    from ai_coding.core.turn_close import terminal_outcome
    from ai_coding.domain.turn_digest import render_handoff

    outcome = terminal_outcome(
        TurnDecision(TurnAction.FINISH, "iteration budget exhausted"),
        budget_expired=False,
        last_text=None,
        iterations=10,
    )

    text = render_handoff(outcome=outcome)

    assert "[回合未完成" in text
    assert "收尾" not in text
