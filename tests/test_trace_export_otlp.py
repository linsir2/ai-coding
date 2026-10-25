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


def test_r9_the_export_is_deterministic(tmp_path):
    view = _view(tmp_path, [_row(), _row(span="c1", parent="i1", evt="ToolStarted")])
    first = json.dumps(rows_to_otlp([view]), sort_keys=True)
    second = json.dumps(rows_to_otlp([view]), sort_keys=True)
    assert first == second
