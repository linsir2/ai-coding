"""TDD (R1a-3): loop event contract + observers.

Events are the loop's only outward channel.  They are frozen values (they record
things that already happened) and they are the reason a 10MB tool output can
never leak into the UI or a log line.
"""

import dataclasses

import pytest

from ai_coding.core.events import (
    OUTPUT_PREVIEW_CHARS,
    AssistantMessage,
    AssistantTextDelta,
    IterationStarted,
    NullObserver,
    RecordingObserver,
    TaskUpdated,
    ToolApprovalRequested,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    preview,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.task import TaskState


def _all_events() -> list[object]:
    ref = ToolCallRef(id="c1", name="read", arguments="{}")
    return [
        IterationStarted(index=1, max_iterations=10),
        AssistantTextDelta(text="hi"),
        AssistantMessage(text="hi", tool_calls=(ref,)),
        ToolApprovalRequested(tool_name="write", params={"path": "a"}, reason="needs confirmation"),
        ToolStarted(call_id="c1", tool_name="read", params={}),
        ToolFinished(
            call_id="c1", tool_name="read", success=True, output_preview="ok", duration_ms=3
        ),
        TaskUpdated(task=TaskState(goal="g")),
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)),
    ]


def test_events_are_frozen():
    event = ToolStarted(call_id="c1", tool_name="read", params={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.tool_name = "write"  # type: ignore[misc]
    finished = ToolFinished(
        call_id="c1", tool_name="read", success=True, output_preview="", duration_ms=0
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        finished.success = False  # type: ignore[misc]


def test_event_fields_carry_through():
    event = ToolFinished(
        call_id="c1",
        tool_name="read",
        success=False,
        output_preview="boom",
        duration_ms=12,
    )
    assert event.call_id == "c1"
    assert event.tool_name == "read"
    assert event.success is False
    assert event.output_preview == "boom"
    assert event.duration_ms == 12


def test_null_observer_never_raises():
    observer = NullObserver()
    for event in _all_events():
        assert observer.on_event(event) is None  # type: ignore[arg-type]


def test_recording_observer_collects_and_filters():
    observer = RecordingObserver()
    for event in _all_events():
        observer.on_event(event)  # type: ignore[arg-type]

    assert len(observer.events) == len(_all_events())
    finished = observer.of_type(ToolFinished)
    assert len(finished) == 1
    assert finished[0].call_id == "c1"
    assert observer.of_type(AssistantTextDelta)[0].text == "hi"
    # filtering returns only the requested type, never the whole stream
    started = observer.of_type(ToolStarted)
    assert len(started) == 1
    assert started[0].tool_name == "read"


def test_preview_truncates_long_text():
    assert preview("short") == "short"
    assert preview("") == ""

    long_text = "x" * (OUTPUT_PREVIEW_CHARS * 3)
    out = preview(long_text)
    assert len(out) <= OUTPUT_PREVIEW_CHARS + 1
    assert out.endswith("…")


def test_preview_honours_custom_limit():
    assert preview("abcdef", limit=3) == "abc…"
    assert preview("abc", limit=3) == "abc"
