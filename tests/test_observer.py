"""BodyObserver: writes loop events to BodyBuffer."""

from ai_coding.core.events import (
    IterationStarted,
    ToolStarted,
    ToolFinished,
    ToolChunk,
    TurnFinished,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.ui.body import BodyBuffer
from ai_coding.ui.observer import BodyObserver


def test_writes_iteration_started():
    body = BodyBuffer()
    obs = BodyObserver(body)
    obs.on_event(IterationStarted(index=1, max_iterations=10))
    assert "[turn 1/10]" in body.get_formatted_text()


def test_writes_tool_started():
    body = BodyBuffer()
    obs = BodyObserver(body)
    obs.on_event(ToolStarted(call_id="1", tool_name="bash", params={"cmd": "ls"}))
    assert "[tool: bash]" in body.get_formatted_text()


def test_writes_tool_finished_ok():
    body = BodyBuffer()
    obs = BodyObserver(body)
    obs.on_event(ToolFinished(call_id="1", tool_name="bash", success=True, output_preview="file.txt"))
    assert "-> ok" in body.get_formatted_text()


def test_writes_tool_finished_failed():
    body = BodyBuffer()
    obs = BodyObserver(body)
    obs.on_event(ToolFinished(call_id="1", tool_name="bash", success=False, error="timeout"))
    assert "-> failed" in body.get_formatted_text()


def test_writes_turn_finished_timeout():
    body = BodyBuffer()
    obs = BodyObserver(body)
    outcome = TurnOutcome(status=TurnStatus.TIMEOUT, reason="max iterations")
    obs.on_event(TurnFinished(outcome=outcome))
    assert "timed out" in body.get_formatted_text()


def test_ignores_turn_finished_normal():
    body = BodyBuffer()
    obs = BodyObserver(body)
    outcome = TurnOutcome(status=TurnStatus.COMPLETED, reason="")
    obs.on_event(TurnFinished(outcome=outcome))
    assert body.get_formatted_text() == ""


def test_throttles_tool_chunks():
    body = BodyBuffer()
    obs = BodyObserver(body, clock=lambda: 0.0)
    obs.on_event(ToolChunk(call_id="1", tool_name="bash", stream="stdout", text="hello", seq=1))
    obs.on_event(ToolChunk(call_id="1", tool_name="bash", stream="stdout", text="world", seq=2))
    text = body.get_formatted_text()
    assert "hello" in text
    assert "world" not in text
