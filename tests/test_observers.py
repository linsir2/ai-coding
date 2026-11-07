"""TDD (R4a): observer fan-out and scoped observers.

The loop talks to exactly one observer.  ``Braid`` lets several listen (a
console, a trace recorder, an accounting ledger) and guarantees that one of them
misbehaving can never affect the others or the turn.  ``ScopedObserver`` is how a
sub-agent's inner loop reports into the *parent's* stream with its parentage
intact (docs/r4-analysis.md 4.6).
"""

from ai_coding.core.events import (
    IterationStarted,
    LoopEvent,
    RecordingObserver,
    ScopedEvent,
    ToolStarted,
)
from ai_coding.core.observers import Braid, SafeObserver, ScopedObserver


def test_braid_forwards_to_every_observer_in_order():
    first, second = RecordingObserver(), RecordingObserver()
    braid = Braid(first, second)

    braid.on_event(IterationStarted(index=1, max_iterations=3))

    assert len(first.events) == 1
    assert len(second.events) == 1
    assert first.events[0] is second.events[0]


def test_a_failing_observer_cannot_stop_the_others():
    seen: list[LoopEvent] = []

    class Boom:
        def on_event(self, event: LoopEvent) -> None:
            raise RuntimeError("renderer exploded")

    class Sink:
        def on_event(self, event: LoopEvent) -> None:
            seen.append(event)

    braid = Braid(Boom(), Sink())
    braid.on_event(IterationStarted(index=1, max_iterations=1))

    assert len(seen) == 1


def test_an_empty_braid_is_harmless():
    Braid().on_event(IterationStarted(index=1, max_iterations=1))


class _Boom:
    def on_event(self, event: LoopEvent) -> None:
        raise RuntimeError("renderer exploded")


def test_a_braid_counts_its_own_swallowed_failures():
    """X-02c: without this counter the TurnReporter reads 0 and never reports
    the degradation, because the Braid is the observer it sees."""
    braid = Braid(_Boom(), RecordingObserver())

    braid.on_event(IterationStarted(index=1, max_iterations=1))

    assert braid.failures == 1


def test_a_braid_aggregates_safe_observers_without_double_counting():
    safe = SafeObserver(_Boom())
    braid = Braid(safe, RecordingObserver())

    braid.on_event(IterationStarted(index=1, max_iterations=1))

    assert safe.failures == 1
    assert braid.failures == 1, "the child counted it; the parent must not add again"


def test_a_nested_braid_is_counted_once():
    inner = Braid(_Boom())
    outer = Braid(inner)

    outer.on_event(IterationStarted(index=1, max_iterations=1))

    assert inner.failures == 1
    assert outer.failures == 1


def test_a_healthy_braid_reports_zero_failures():
    braid = Braid(RecordingObserver(), SafeObserver(RecordingObserver()))

    braid.on_event(IterationStarted(index=1, max_iterations=1))

    assert braid.failures == 0


def test_scoped_observer_stamps_scope_and_parent():
    inner = RecordingObserver()
    scoped = ScopedObserver(scope="subagent:c9", parent="c9", inner=inner)

    scoped.on_event(ToolStarted(call_id="c2", tool_name="read", params={}))

    assert len(inner.events) == 1
    wrapped = inner.events[0]
    assert isinstance(wrapped, ScopedEvent)
    assert wrapped.scope == "subagent:c9"
    assert wrapped.parent == "c9"
    assert isinstance(wrapped.event, ToolStarted)
    assert wrapped.event.call_id == "c2"
