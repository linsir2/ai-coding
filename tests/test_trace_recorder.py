"""TDD (R4a): the trace recorder.

It is an ordinary ``LoopObserver``: one JSON line per event, secrets redacted,
spans/parents labelled, and — the part that matters most — it can fail without
anyone noticing (docs/r4-analysis.md 4.2, P1).
"""

import json

from ai_coding.core.events import (
    ContextAssembled,
    Degraded,
    IterationStarted,
    ModelStepFinished,
    ScopedEvent,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from ai_coding.core.trace import TraceRecorder, TraceStore
from ai_coding.domain.loop import TurnOutcome, TurnStatus


def _recorder(tmp_path, **over):
    kwargs = {"session_id": "sess-1"}
    kwargs.update(over)
    store = TraceStore(tmp_path / "traces", max_turn_bytes=1_000_000)
    return TraceRecorder(store, **kwargs), store


def _lines(recorder):
    path = recorder.current_path
    assert path is not None
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_every_event_becomes_one_json_line(tmp_path):
    recorder, _ = _recorder(tmp_path)
    recorder.on_event(IterationStarted(index=1, max_iterations=5))
    recorder.on_event(ModelStepFinished(index=1, usage={"prompt_tokens": 10}, latency_ms=5))

    lines = _lines(recorder)
    assert [line["evt"] for line in lines] == ["IterationStarted", "ModelStepFinished"]
    first = lines[0]
    assert first["v"] == 1
    assert first["session"] == "sess-1"
    assert first["turn"] == 1
    assert first["span"] == "i1"
    assert first["parent"] == "turn"
    assert first["iter"] == 1
    assert first["data"]["max_iterations"] == 5
    assert first["ts"].endswith("Z")


def test_tool_spans_hang_off_their_iteration(tmp_path):
    recorder, _ = _recorder(tmp_path)
    recorder.on_event(ToolStarted(call_id="c1", tool_name="read", params={}, iteration=2))
    recorder.on_event(
        ToolFinished(
            call_id="c1",
            tool_name="read",
            success=True,
            output_preview="ok",
            duration_ms=4,
            iteration=2,
        )
    )

    for line in _lines(recorder):
        assert line["span"] == "c1"
        assert line["parent"] == "i2"
        assert line["iter"] == 2


def test_scoped_events_are_unwrapped_with_their_parent(tmp_path):
    recorder, _ = _recorder(tmp_path)
    inner = ToolStarted(call_id="c2", tool_name="glob", params={})
    recorder.on_event(ScopedEvent(scope="subagent:c9", parent="c9", event=inner))

    line = _lines(recorder)[0]
    assert line["evt"] == "ToolStarted"
    assert line["scope"] == "subagent:c9"
    assert line["parent"] == "c9"
    assert line["data"]["call_id"] == "c2"


def test_secrets_never_reach_the_file(tmp_path):
    recorder, _ = _recorder(tmp_path)
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    recorder.on_event(
        ToolStarted(call_id="c1", tool_name="bash", params={"command": f"echo {secret}"})
    )

    raw = recorder.current_path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "redacted" in raw


def test_a_broken_disk_never_raises(tmp_path):
    # ``traces`` is a file, so no session directory (and no trace file) can exist
    blocked = tmp_path / "traces"
    blocked.write_text("not a directory", encoding="utf-8")
    recorder = TraceRecorder(TraceStore(blocked), session_id="sess-1")

    recorder.on_event(IterationStarted(index=1, max_iterations=1))

    assert recorder.failures >= 1


def test_previews_mode_trims_long_payloads(tmp_path):
    recorder, _ = _recorder(tmp_path, capture="previews")
    recorder.on_event(
        ToolFinished(
            call_id="c1",
            tool_name="read",
            success=True,
            output_preview="x" * 5_000,
            duration_ms=1,
        )
    )
    line = _lines(recorder)[0]
    assert len(line["data"]["output_preview"]) <= 520


def test_full_mode_keeps_what_the_event_carries(tmp_path):
    recorder, _ = _recorder(tmp_path, capture="full")
    recorder.on_event(
        ToolFinished(
            call_id="c1",
            tool_name="read",
            success=True,
            output_preview="x" * 5_000,
            duration_ms=1,
        )
    )
    line = _lines(recorder)[0]
    assert len(line["data"]["output_preview"]) == 5_000


def test_turn_finished_ends_the_turn_and_records_a_summary(tmp_path):
    recorder, store = _recorder(tmp_path)
    recorder.on_event(IterationStarted(index=1, max_iterations=1))
    recorder.on_event(ContextAssembled(iteration=1, system_chars=10))
    recorder.on_event(
        ModelStepFinished(
            index=1,
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            cached_tokens=80,
            latency_ms=12,
        )
    )
    recorder.on_event(
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, iterations=1))
    )
    first_turn_path = recorder.current_path

    recorder.on_event(IterationStarted(index=1, max_iterations=1))

    assert recorder.current_path != first_turn_path
    assert recorder.current_path.name == "turn-2.jsonl"
    summary = json.loads(store.summary_path("sess-1").read_text(encoding="utf-8"))
    assert summary["turns"] == 1
    assert summary["prompt_tokens"] == 100
    assert summary["cached_tokens"] == 80


def test_degraded_events_are_recorded_like_any_other(tmp_path):
    recorder, _ = _recorder(tmp_path)
    recorder.on_event(Degraded(component="memory_block", reason="boom", fallback="none"))
    line = _lines(recorder)[0]
    assert line["evt"] == "Degraded"
    assert line["data"]["component"] == "memory_block"


def test_post_turn_events_do_not_invent_a_new_turn(tmp_path):
    """Harvest/consolidation arrive after TurnFinished — same turn, same file."""
    from ai_coding.core.events import MemoryHarvested

    recorder, _ = _recorder(tmp_path)
    recorder.on_event(IterationStarted(index=1, max_iterations=1))
    recorder.on_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)))
    recorder.on_event(MemoryHarvested(added=("n1",)))

    assert recorder.current_path.name == "turn-1.jsonl"
    assert [line["evt"] for line in _lines(recorder)] == [
        "IterationStarted",
        "TurnFinished",
        "MemoryHarvested",
    ]
    # a real next turn still gets its own file
    recorder.on_event(IterationStarted(index=1, max_iterations=1))
    assert recorder.current_path.name == "turn-2.jsonl"
