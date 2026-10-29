"""TDD (R9d): recorded rows become OTLP/JSON spans — deterministic, no network."""

import json
from pathlib import Path

from ai_coding.core.export_html import SessionView, read_session_view
from ai_coding.core.export_otlp import rows_to_otlp
from ai_coding.core.trace import TraceStore


def _row(**over) -> dict:
    base = {
        "v": 1, "ts": "2026-09-28T00:00:00.000Z", "session": "s", "turn": 1,
        "span": "i1", "parent": "turn", "iter": 1, "evt": "IterationStarted", "data": {},
    }
    base.update(over)
    return base


def _view(tmp_path: Path, rows: list[dict]) -> SessionView:
    store = TraceStore(tmp_path / "traces", workspace=tmp_path)
    writer = store.begin_turn("s")
    for row in rows:
        writer.write_line(json.dumps(row))
    return read_session_view(store, "s")


def _spans(payload: dict) -> list[dict]:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_r9_envelope_carries_the_service_and_scope(tmp_path):
    payload = rows_to_otlp([_view(tmp_path, [_row()])])
    resource = payload["resourceSpans"][0]["resource"]
    keys = {attr["key"] for attr in resource["attributes"]}
    assert "service.name" in keys and "ai-coding.session" in keys
    scope = payload["resourceSpans"][0]["scopeSpans"][0]["scope"]
    assert scope["name"] == "ai-coding.trace"


def test_r9_parent_ids_resolve_to_the_parent_span(tmp_path):
    view = _view(tmp_path, [
        _row(span="turn", parent="", evt="TurnFinished"),
        _row(span="i1", parent="turn", evt="IterationStarted"),
        _row(span="step1", parent="i1", evt="ModelStepFinished",
             data={"latency_ms": 120, "usage": {"prompt_tokens": 5}}),
    ])
    spans = {span["name"]: span for span in _spans(rows_to_otlp([view]))}
    turn = spans["TurnFinished"]
    iteration = spans["IterationStarted"]
    step = spans["ModelStepFinished"]
    assert turn["parentSpanId"] == ""
    assert iteration["parentSpanId"] == turn["spanId"]
    assert step["parentSpanId"] == iteration["spanId"]


def test_r9_timestamps_use_nanos_and_latency_sets_the_end(tmp_path):
    view = _view(tmp_path, [_row(evt="ModelStepFinished", data={"latency_ms": 250})])
    span = _spans(rows_to_otlp([view]))[0]
    start = int(span["startTimeUnixNano"])
    assert start > 0
    assert int(span["endTimeUnixNano"]) == start + 250 * 1_000_000


def test_r9_instant_events_are_marked_and_never_zero_length(tmp_path):
    view = _view(tmp_path, [_row()])
    span = _spans(rows_to_otlp([view]))[0]
    attrs = {attr["key"]: attr["value"] for attr in span["attributes"]}
    assert attrs["ai-coding.instant"]["boolValue"] is True
    assert int(span["endTimeUnixNano"]) == int(span["startTimeUnixNano"]) + 1


def test_r9_attribute_types_are_mapped(tmp_path):
    view = _view(tmp_path, [
        _row(evt="ToolFinished", span="c1",
             data={"tool_name": "bash", "success": True, "duration_ms": 42,
                   "params": {"command": "ls"}}),
    ])
    span = _spans(rows_to_otlp([view]))[0]
    attrs = {attr["key"]: attr["value"] for attr in span["attributes"]}
    assert attrs["tool_name"] == {"stringValue": "bash"}
    assert attrs["success"] == {"boolValue": True}
    assert attrs["duration_ms"] == {"intValue": "42"}
    assert attrs["params"] == {"stringValue": '{"command": "ls"}'}  # nested → JSON text


def test_r9_a_tool_error_becomes_an_error_status(tmp_path):
    view = _view(tmp_path, [_row(evt="ToolFinished", span="c1", data={"error": "nonzero_exit"})])
    span = _spans(rows_to_otlp([view]))[0]
    assert span["status"]["code"] == 2


def test_a13_a_span_is_named_by_its_first_event(tmp_path):
    """A-13: the span name is one operation name, not the whole event log."""
    view = _view(tmp_path, [
        _row(evt="IterationStarted"),
        _row(evt="ContextAssembled"),
        _row(evt="AssistantMessage"),
        _row(span="c1", evt="ToolStarted"),
        _row(span="c1", evt="ToolFinished"),
    ])
    spans = {span["spanId"]: span for span in _spans(rows_to_otlp([view]))}
    by_span = {
        attr["value"]["stringValue"]: span
        for span in spans.values()
        for attr in span["attributes"]
        if attr["key"] == "ai-coding.span"
    }
    iteration = by_span["i1"]
    tool = by_span["c1"]
    assert iteration["name"] == "IterationStarted"
    assert "|" not in iteration["name"]
    events = {attr["key"]: attr["value"] for attr in iteration["attributes"]}["ai-coding.events"]
    assert events == {"stringValue": "IterationStarted|ContextAssembled|AssistantMessage"}
    assert tool["name"] == "ToolStarted"


def test_a13_the_name_is_the_earliest_event_not_the_first_written_line(tmp_path):
    """A skew or a resumed writer can put a later event on disk first."""
    view = _view(tmp_path, [
        _row(span="c1", evt="ToolFinished", ts="2026-09-28T00:00:02.000Z"),
        _row(span="c1", evt="ToolStarted", ts="2026-09-28T00:00:01.000Z"),
    ])
    assert _spans(rows_to_otlp([view]))[0]["name"] == "ToolStarted"


def test_r9_the_export_is_deterministic(tmp_path):
    view = _view(tmp_path, [_row(), _row(span="c1", parent="i1", evt="ToolStarted")])
    first = json.dumps(rows_to_otlp([view]), sort_keys=True)
    second = json.dumps(rows_to_otlp([view]), sort_keys=True)
    assert first == second


# ---- A-16: a sub-agent's spans are their own spans --------------------------


def _attrs(span: dict) -> dict:
    return {attr["key"]: attr["value"] for attr in span["attributes"]}


def _text(span: dict, key: str) -> str:
    return _attrs(span).get(key, {}).get("stringValue", "")


def _scope_span_id(session: str, turn: int, span: str, scope: str) -> str:
    import hashlib

    return hashlib.sha256(f"{session}|{turn}|{scope}|{span}".encode()).hexdigest()[:16]


def test_a16_a_subagent_span_is_not_merged_into_its_parent(tmp_path):
    scope = "subagent:child:c1"
    view = _view(tmp_path, [
        _row(span="i1", parent="turn", evt="IterationStarted"),
        _row(span="i1", parent="turn", evt="IterationStarted", scope=scope),
    ])
    spans = [span for span in _spans(rows_to_otlp([view])) if _text(span, "ai-coding.span") == "i1"]
    assert len(spans) == 2
    assert spans[0]["spanId"] != spans[1]["spanId"]
    scoped = [span for span in spans if "ai-coding.scope" in _attrs(span)]
    assert len(scoped) == 1
    assert _text(scoped[0], "ai-coding.scope") == scope


def test_a16_the_subagent_root_points_at_the_spawning_call(tmp_path):
    scope = "subagent:child:c1"
    view = _view(tmp_path, [
        _row(span="turn", parent="", evt="TurnFinished"),
        _row(span="call_fd", parent="i1", evt="ToolFinished"),
        _row(span="turn", parent="call_fd", scope=scope, evt="TurnFinished"),
    ])
    spans = _spans(rows_to_otlp([view]))
    root_turn = next(
        span for span in spans
        if _text(span, "ai-coding.span") == "turn" and "ai-coding.scope" not in _attrs(span)
    )
    call = next(span for span in spans if _text(span, "ai-coding.span") == "call_fd")
    child_turn = next(span for span in spans if _text(span, "ai-coding.scope") == scope)
    assert child_turn["parentSpanId"] == call["spanId"]
    assert child_turn["parentSpanId"] != root_turn["spanId"]


def test_a16_the_subagent_inner_tree_survives_export(tmp_path):
    scope = "subagent:child:c1"
    view = _view(tmp_path, [
        _row(span="turn", parent="", evt="TurnFinished"),
        _row(span="i1", parent="turn", evt="IterationStarted"),
        _row(span="call_fd", parent="i1", evt="ToolFinished"),
        _row(span="turn", parent="call_fd", scope=scope, evt="TurnFinished"),
        _row(span="i1", parent="turn", scope=scope, evt="IterationStarted"),
        _row(span="step1", parent="i1", scope=scope, evt="ModelStepFinished"),
        _row(span="call_x", parent="i1", scope=scope, evt="ToolStarted"),
    ])
    by = {
        (_text(span, "ai-coding.scope"), _text(span, "ai-coding.span")): span
        for span in _spans(rows_to_otlp([view]))
    }
    assert by[("", "turn")]["parentSpanId"] == ""
    assert by[("", "i1")]["parentSpanId"] == by[("", "turn")]["spanId"]
    assert by[("", "call_fd")]["parentSpanId"] == by[("", "i1")]["spanId"]
    assert by[(scope, "turn")]["parentSpanId"] == by[("", "call_fd")]["spanId"]
    assert by[(scope, "i1")]["parentSpanId"] == by[(scope, "turn")]["spanId"]
    assert by[(scope, "step1")]["parentSpanId"] == by[(scope, "i1")]["spanId"]
    assert by[(scope, "call_x")]["parentSpanId"] == by[(scope, "i1")]["spanId"]


def test_a16_old_flat_subagent_rows_still_export(tmp_path):
    scope = "subagent:child:c1"
    view = _view(tmp_path, [
        _row(span="call_fd", parent="i1", evt="ToolFinished"),
        _row(span="i1", parent="call_fd", scope=scope, evt="IterationStarted"),
        _row(span="step1", parent="call_fd", scope=scope, evt="ModelStepFinished"),
        _row(span="turn", parent="call_fd", scope=scope, evt="TurnFinished"),
    ])
    by = {
        (_text(span, "ai-coding.scope"), _text(span, "ai-coding.span")): span
        for span in _spans(rows_to_otlp([view]))
    }
    for span_name in ("i1", "step1", "turn"):
        assert by[(scope, span_name)]["parentSpanId"] == by[("", "call_fd")]["spanId"]


def test_a16_a_truncated_child_parent_stays_in_its_own_scope(tmp_path):
    """A child trace cut before its ``turn`` row must not attach to the root."""
    scope = "subagent:child:c1"
    view = _view(tmp_path, [
        _row(span="turn", parent="", evt="TurnFinished"),
        _row(span="i1", parent="turn", scope=scope, evt="IterationStarted"),
    ])
    by = {
        (_text(span, "ai-coding.scope"), _text(span, "ai-coding.span")): span
        for span in _spans(rows_to_otlp([view]))
    }
    assert by[(scope, "i1")]["parentSpanId"] != by[("", "turn")]["spanId"]
    assert by[(scope, "i1")]["parentSpanId"] == _scope_span_id("s", 1, "turn", scope)


def test_a16_each_scope_keeps_its_own_status(tmp_path):
    scope = "subagent:child:c1"
    view = _view(tmp_path, [
        _row(span="i1", parent="turn", evt="IterationStarted"),
        _row(span="i1", parent="turn", scope=scope, evt="IterationStarted",
             data={"error": "x"}),
    ])
    spans = [span for span in _spans(rows_to_otlp([view])) if _text(span, "ai-coding.span") == "i1"]
    root = next(span for span in spans if "ai-coding.scope" not in _attrs(span))
    child = next(span for span in spans if _text(span, "ai-coding.scope") == scope)
    assert root["status"]["code"] == 0
    assert child["status"]["code"] == 2


def test_a16_unscoped_span_ids_are_unchanged(tmp_path):
    import hashlib

    payload = rows_to_otlp([_view(tmp_path, [_row(evt="IterationStarted")])])
    span = _spans(payload)[0]
    assert span["spanId"] == hashlib.sha256(b"s|1|i1").hexdigest()[:16]


def test_a16_two_scopes_with_the_same_name_get_distinct_ids(tmp_path):
    view = _view(tmp_path, [
        _row(span="i1", parent="turn", evt="IterationStarted"),
        _row(span="i1", parent="turn", scope="subagent:a:c1", evt="IterationStarted"),
        _row(span="i1", parent="turn", scope="subagent:b:c2", evt="IterationStarted"),
    ])
    ids = [span["spanId"] for span in _spans(rows_to_otlp([view]))]
    assert len(ids) == 3
    assert len(set(ids)) == 3
