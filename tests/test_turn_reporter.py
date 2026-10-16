"""TDD (R5): the turn reporter — events, ledger and degradation in one unit.

This is a pure extraction: the same events, in the same order, with the same
fields the loop used to emit inline (docs/r5-analysis.md 3.1).  The point of
moving it is that "what does this turn tell the world?" becomes one named thing —
and that "report a degradation" stops being copied into three modules.
"""

import pytest

from ai_coding.core.budget import BudgetLedger
from ai_coding.core.events import (
    ContextAssembled,
    Degraded,
    ModelStepFinished,
    RecordingObserver,
    TurnFinished,
)
from ai_coding.core.trace import canonical_request
from ai_coding.core.turn_report import TurnReporter, report_degraded
from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.domain.message import ChatMessage

TOOLS_TEXT = '[{"type":"function","function":{"name":"read"}}]'


def _reporter(observer: RecordingObserver | None = None) -> tuple[TurnReporter, RecordingObserver]:
    sink = observer or RecordingObserver()
    return TurnReporter(tools_text=TOOLS_TEXT, emit=sink.on_event), sink


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="hello", timestamp=""),
        ChatMessage(role="assistant", content="hi", timestamp=""),
    ]


# ---- the shared degradation reporter ---------------------------------------


def test_report_degraded_reaches_the_observer():
    sink = RecordingObserver()
    report_degraded(sink, "memory_block", "boom", "no block")
    event = sink.of_type(Degraded)[0]
    assert event.component == "memory_block"
    assert event.reason == "boom"
    assert event.fallback == "no block"


def test_report_degraded_swallows_a_broken_observer():
    class Boom:
        def on_event(self, event):  # type: ignore[no-untyped-def]
            raise RuntimeError("observer exploded")

    report_degraded(Boom(), "x", "y")  # must not raise


def test_report_degraded_tolerates_a_missing_observer():
    report_degraded(None, "x", "y")  # must not raise


def test_report_degraded_is_what_mcp_and_subagent_use():
    """The shared function replaces three private copies of the same idea."""
    sink = RecordingObserver()
    for component in ("mcp_server", "worktree_isolate"):
        report_degraded(sink, component, "reason", "fallback")
    assert [e.component for e in sink.of_type(Degraded)] == [
        "mcp_server",
        "worktree_isolate",
    ]


# ---- TurnReporter ----------------------------------------------------------


def test_tools_chars_matches_the_schema_text():
    reporter, _ = _reporter()
    assert reporter.tools_chars == len(TOOLS_TEXT)


def test_report_context_emits_the_same_fields_the_loop_used_to():
    reporter, sink = _reporter()
    history = _messages()
    tail = [ChatMessage(role="user", content="task block", timestamp="")]
    request = [*history, *tail]

    canonical = reporter.report_context(
        iteration=1,
        instructions="SYS",
        history=history,
        request=request,
        tail_blocks=(("task", len("task block")),),
        previous=None,
    )

    event = sink.of_type(ContextAssembled)[0]
    assert event.iteration == 1
    assert event.system_chars == len("SYS")
    assert event.tools_chars == len(TOOLS_TEXT)
    assert event.history_messages == 2
    assert event.history_chars == len("hello") + len("hi")
    assert event.tail_blocks == (("task", len("task block")),)
    assert event.prefix_shared_chars == 0
    assert event.prefix_shared_ratio == 0.0
    assert event.digest
    assert event.estimated_tokens > 0
    # the returned canonical string is exactly what the next call compares against
    assert canonical == canonical_request("SYS", request, TOOLS_TEXT)


def test_report_context_measures_the_shared_prefix_against_the_previous_request():
    reporter, sink = _reporter()
    history = _messages()
    first = reporter.report_context(
        iteration=1, instructions="SYS", history=history, request=list(history),
        tail_blocks=(), previous=None,
    )

    reporter.report_context(
        iteration=2, instructions="SYS",
        history=[*history, ChatMessage(role="user", content="more", timestamp="")],
        request=[*history, ChatMessage(role="user", content="more", timestamp="")],
        tail_blocks=(), previous=first,
    )

    second = sink.of_type(ContextAssembled)[1]
    assert second.prefix_shared_chars > 0
    assert 0.0 < second.prefix_shared_ratio <= 1.0


def test_report_step_emits_usage_and_feeds_the_ledger():
    reporter, sink = _reporter()
    step = StepResult(
        text="done",
        finish_reason="stop",
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "cached_tokens": 80,
        },
    )

    reporter.report_step(index=2, step=step, latency_ms=1234)

    event = sink.of_type(ModelStepFinished)[0]
    assert event.index == 2
    assert event.finish_reason == "stop"
    assert event.text_chars == len("done")
    assert event.tool_call_count == 0
    assert event.cached_tokens == 80
    assert event.latency_ms == 1234
    assert reporter.ledger.totals.prompt_tokens == 100
    assert reporter.ledger.totals.completion_tokens == 10
    assert reporter.tokens_spent() == 110
    assert reporter.ledger.cache_hit_ratio == 0.8


def test_report_step_survives_a_missing_usage():
    reporter, sink = _reporter()
    reporter.report_step(index=1, step=StepResult(text="hi"), latency_ms=5)
    event = sink.of_type(ModelStepFinished)[0]
    assert event.usage is None
    assert reporter.ledger.totals.steps == 1


def test_report_degraded_method_and_ledger_exposure():
    reporter, sink = _reporter()
    assert isinstance(reporter.ledger, BudgetLedger)
    reporter.report_degraded("persist", "disk full", "memory only")
    assert sink.of_type(Degraded)[0].component == "persist"


def test_finish_turn_attaches_the_usage_and_emits_last():
    reporter, sink = _reporter()
    observer = RecordingObserver()
    reporter.begin_turn(observer)
    reporter.report_step(
        index=1,
        step=StepResult(usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
        latency_ms=1,
    )

    outcome = reporter.finish_turn(
        TurnOutcome(status=TurnStatus.COMPLETED, text="done"), observer
    )

    # 19 §3.5: the usage dict also carries the window reading (last step's
    # prompt), always when a step reported numbers.
    assert outcome.usage == {
        "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
        "context_tokens": 7,
    }
    assert isinstance(sink.events[-1], TurnFinished)
    assert sink.of_type(TurnFinished)[0].outcome.usage == outcome.usage


def test_finish_turn_reports_a_broken_observer_before_closing():
    """Order matters: the Degraded line belongs to this turn, so it comes first."""

    class _Flaky:
        failures = 0

        def on_event(self, event):  # type: ignore[no-untyped-def]
            type(self).failures += 1  # simulate a renderer that keeps raising

    reporter, sink = _reporter()
    flaky = _Flaky()
    reporter.begin_turn(flaky)
    flaky.failures = 3  # three failures happened during the turn

    reporter.finish_turn(TurnOutcome(status=TurnStatus.COMPLETED), flaky)

    kinds = [type(e).__name__ for e in sink.events]
    assert kinds == ["Degraded", "TurnFinished"]
    assert sink.of_type(Degraded)[0].component == "observer"


def test_finish_turn_stays_quiet_when_nothing_failed():
    reporter, sink = _reporter()
    observer = RecordingObserver()  # has no ``failures`` attribute at all
    reporter.begin_turn(observer)
    reporter.finish_turn(TurnOutcome(status=TurnStatus.COMPLETED), observer)
    assert [type(e).__name__ for e in sink.events] == ["TurnFinished"]


@pytest.mark.parametrize("status", [TurnStatus.COMPLETED, TurnStatus.ERROR])
def test_finish_turn_preserves_the_status_and_text(status):
    reporter, _ = _reporter()
    observer = RecordingObserver()
    outcome = reporter.finish_turn(
        TurnOutcome(status=status, text="t", iterations=3, reason="r"), observer
    )
    assert outcome.status is status
    assert outcome.text == "t"
    assert outcome.iterations == 3
    assert outcome.reason == "r"


def test_begin_turn_resets_the_ledger_so_usage_is_per_turn():
    reporter, _sink = _reporter()
    observer = RecordingObserver()

    reporter.begin_turn(observer)
    reporter.report_step(
        index=1,
        step=StepResult(
            usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}
        ),
        latency_ms=1,
    )
    reporter.finish_turn(TurnOutcome(status=TurnStatus.COMPLETED), observer)

    reporter.begin_turn(observer)
    reporter.report_step(
        index=1,
        step=StepResult(usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
        latency_ms=1,
    )
    second = reporter.finish_turn(TurnOutcome(status=TurnStatus.COMPLETED), observer)

    assert second.usage == {
        "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
        "context_tokens": 7,
    }
    assert reporter.tokens_spent() == 10
