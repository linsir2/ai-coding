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


def test_streamed_tool_output_is_not_recorded(tmp_path):
    """ToolChunk is the one event that grows with the output; skip it.

    A call still announces itself twice — ``ToolStarted`` with its params and
    ``ToolFinished`` with its result — so the timeline keeps the call and its
    outcome.  What a chunk adds is volume: one line per fragment, so a build log
    arriving in 4KB pieces is a few hundred lines inside the same 2MB turn
    budget, crowding out the events that explain what the turn did.  Measured on
    a real two-turn run, everything else is 15 events / 3 KiB per turn.

    The live view is unaffected: chunks still reach the transcript fold, which is
    a different sink.
    """
    from ai_coding.core.events import ToolChunk

    recorder, store = _recorder(tmp_path)
    recorder.on_event(ToolStarted(call_id="c1", tool_name="bash",
                                 params={"command": "npm ci"}, iteration=0))
    recorder.on_event(ToolChunk(call_id="c1", tool_name="bash", stream="stdout",
                                text="x" * 4000, seq=0))
    recorder.on_event(ToolFinished(call_id="c1", tool_name="bash", success=True,
                                  output_preview="done", duration_ms=5, iteration=0))
    recorder.on_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)))

    lines = store.session_dir("sess-1").glob("turn-*.jsonl")
    written = [
        json.loads(line)
        for path in lines
        for line in path.read_text().splitlines()
    ]
    kinds = [entry["evt"] for entry in written]
    assert "ToolStarted" in kinds and "ToolFinished" in kinds
    assert "ToolChunk" not in kinds


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


def test_full_capture_snapshots_the_turn_from_the_session(tmp_path):
    """``capture: full`` reads the *source of truth*, not the display preview.

    Events carry a 500-char preview because they are the display channel; the
    full text only exists in the session.  So a full capture is a snapshot of the
    turn's messages — redacted, and still bounded (docs/r4-analysis.md 4.2).
    """
    from ai_coding.domain.message import ChatMessage
    from ai_coding.domain.session import SessionData

    recorder, _ = _recorder(tmp_path, capture="full")
    session = SessionData(
        session_id="sess-1", title="", created_time="", last_access_time=""
    )
    recorder.attach_session(session, start=0)
    session.add_message(ChatMessage(role="user", content="hello", timestamp=""))
    session.add_message(
        ChatMessage(
            role="tool",
            content="x" * 20_000,
            timestamp="",
            tool_call_id="c1",
            tool_name="read",
        )
    )
    recorder.on_event(IterationStarted(index=1, max_iterations=1))
    recorder.on_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)))

    payload = recorder.last_payload_path
    assert payload is not None
    assert payload.name == "turn-1.full.json"
    data = json.loads(payload.read_text(encoding="utf-8"))
    assert [m["role"] for m in data["messages"]] == ["user", "tool"]
    assert len(data["messages"][1]["content"]) == 20_000  # the whole thing
    assert data["turn"] == 1


def test_full_capture_is_off_by_default(tmp_path):
    recorder, _ = _recorder(tmp_path, capture="previews")
    recorder.on_event(IterationStarted(index=1, max_iterations=1))
    recorder.on_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)))
    assert recorder.last_payload_path is None


def test_full_capture_redacts_the_snapshot(tmp_path):
    from ai_coding.domain.message import ChatMessage
    from ai_coding.domain.session import SessionData

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    recorder, _ = _recorder(tmp_path, capture="full")
    session = SessionData(
        session_id="sess-1", title="", created_time="", last_access_time=""
    )
    recorder.attach_session(session, start=0)
    session.add_message(
        ChatMessage(role="tool", content=f"KEY={secret}", timestamp="", tool_call_id="c1")
    )
    recorder.on_event(IterationStarted(index=1, max_iterations=1))
    recorder.on_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)))

    assert recorder.last_payload_path is not None
    assert secret not in recorder.last_payload_path.read_text(encoding="utf-8")


def test_a_sub_agents_streamed_output_is_not_recorded(tmp_path):
    """The chunk guard judged the event as it arrived.

    A sub-agent's events reach the recorder wrapped in ``ScopedEvent``, so
    ``isinstance(event, ToolChunk)`` was False and every streamed fragment of a
    child call landed on disk — 37 rows, all scoped, in one real session.  The
    reason for dropping chunks is the turn's byte budget, which does not care
    who asked.
    """
    from ai_coding.core.events import ToolChunk

    recorder, store = _recorder(tmp_path)
    recorder.on_event(ToolStarted(call_id="c1", tool_name="bash",
                                 params={"command": "npm ci"}, iteration=0))
    recorder.on_event(ScopedEvent(
        scope="subagent:probe:c1", parent="c1",
        event=ToolChunk(call_id="c1", tool_name="bash", stream="stdout",
                        text="x" * 4000, seq=0),
    ))
    recorder.on_event(ToolFinished(call_id="c1", tool_name="bash", success=True,
                                  output_preview="done", duration_ms=5, iteration=0))
    recorder.on_event(TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)))

    kinds = [entry["evt"] for entry in _lines(recorder)]
    assert "ToolChunk" not in kinds, kinds
    assert "ToolStarted" in kinds and "ToolFinished" in kinds


def test_a_sub_agents_turn_finishing_does_not_end_the_parent_turn(tmp_path):
    """`TurnFinished` is decided on the *unwrapped* name, so a child ending its
    turn ran the parent's finaliser — and `_finish_turn` never zeroes the
    running totals, so the parent's own `TurnFinished` added them a second time.

    Measured on one real session: summary.json reported 2.00x the
    ModelStepFinished rows actually on disk, 2.54x the prompt tokens.
    """
    recorder, store = _recorder(tmp_path)

    def step() -> ModelStepFinished:
        return ModelStepFinished(
            index=1,
            usage={"prompt_tokens": 100, "completion_tokens": 20,
                   "total_tokens": 120},
            cached_tokens=80,
            latency_ms=5,
        )

    recorder.on_event(IterationStarted(index=1, max_iterations=8))
    recorder.on_event(
        ScopedEvent(scope="subagent:probe:c1", parent="c1", event=step())
    )
    recorder.on_event(
        ScopedEvent(
            scope="subagent:probe:c1", parent="c1",
            event=TurnFinished(
                outcome=TurnOutcome(status=TurnStatus.COMPLETED, iterations=1)
            ),
        )
    )
    recorder.on_event(step())
    recorder.on_event(
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, iterations=1))
    )

    summary = json.loads(store.summary_path("sess-1").read_text(encoding="utf-8"))
    # The child's step is real spend and stays in the parent's total; what must
    # not happen is the child's TurnFinished flushing it early.
    assert summary["turns"] == 1
    assert summary["steps"] == 2
    assert summary["prompt_tokens"] == 200
    assert summary["cached_tokens"] == 160
