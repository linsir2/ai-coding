"""OTLP/JSON export of recorded spans (R9d).

One OTLP span per *trace span* (not per event): a turn's rows are grouped by
``span``, so ``i1`` becomes one span with its events as attributes and its child
``step1``/``<call_id>`` pointing at it through ``parentSpanId``.  Ids are derived
from ``session|turn|span`` with sha256, so exporting the same trace twice is
byte-identical - real OTel SDKs generate random ids; this is a deliberate
difference (see docs/r9-analysis.md 2.2).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from ai_coding.core.export_html import SessionView, SpanRow
from ai_coding.infra.time_utils import parse_iso8601

__all__ = ["rows_to_otlp"]

_SPAN_ID_CHARS = 16
_TRACE_ID_CHARS = 32


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _hash(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _nano(ts: str) -> int:
    moment = parse_iso8601(ts)
    if moment is None:
        return 0
    return int(moment.timestamp() * 1_000_000_000)


def _value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, ensure_ascii=False)}


def _attributes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one row's ``data``; ``None`` is dropped, nesting becomes JSON text."""
    return [
        {"key": str(key), "value": _value(value)}
        for key, value in sorted(payload.items())
        if value is not None
    ]


def _span_id(session: str, turn: int, span: str, scope: str = "") -> str:
    # Without a scope the key is the historical one, byte-for-byte; with a scope
    # the scope is part of the hash so a sub-agent's ``i1`` is its own span.
    key = f"{session}|{turn}|{span}" if not scope else f"{session}|{turn}|{scope}|{span}"
    return _hash(key, _SPAN_ID_CHARS)


#: The span names a child loop builds for itself.  A scoped row whose parent is
#: one of these belongs to the same scope even when the parent row is missing
#: (a truncated trace), never to the root loop's span of the same name.
_INNER_PARENT = re.compile(r"^(?:turn|i\d+|step\d+)$")


def _parent_scope(
    name: str, own_scope: str, scopes_by_span: dict[str, set[str]]
) -> str:
    """Which scope a parent span of ``name`` lives in, from the child's view."""
    if not name or not own_scope:
        return ""
    # A new trace keeps the child's inner tree in the same scope (i1→turn,
    # step1→i1).  Only the child's root points outside it.
    if own_scope in scopes_by_span.get(name, set()):
        return own_scope
    if _INNER_PARENT.match(name):
        # The same-scope parent row is gone (truncated turn file).  Keeping the
        # id inside this scope is a dangling reference; the root loop may have a
        # span with the same name, and attaching to it would be silently wrong.
        return own_scope
    candidates = scopes_by_span.get(name, set()) - {own_scope}
    if not candidates:
        return own_scope
    if "" in candidates:
        # The spawning ``subAgent`` call is in the root scope; old flat traces
        # hung every child row off that call.
        return ""
    return sorted(candidates)[0]


def rows_to_otlp(
    views: Sequence[SessionView], *, service_name: str = "ai-coding"
) -> dict[str, Any]:
    """Render sessions as OTLP/JSON.  Pure; no network (D-R9-5)."""
    resources: list[dict[str, Any]] = []
    for view in views:
        spans: list[dict[str, Any]] = []
        for turn in view.turns:
            grouped: dict[tuple[str, str], list[SpanRow]] = {}
            scopes_by_span: dict[str, set[str]] = {}
            for row in turn.spans:
                grouped.setdefault((row.scope, row.span), []).append(row)
                scopes_by_span.setdefault(row.span, set()).add(row.scope)
            for (scope, span_name), rows in grouped.items():
                events = [row.evt for row in rows]
                start = min(_nano(row.ts) for row in rows)
                # The span's operation name: the event with the earliest ts
                # (ties keep the write order).  Same anchor as ``start``, so a
                # clock skew or an out-of-order line cannot name the span after
                # an event it did not start with.
                first_event = min(rows, key=lambda row: _nano(row.ts)).evt
                duration_ms = max(
                    max(_int(row.data.get("latency_ms")), _int(row.data.get("duration_ms")))
                    for row in rows
                )
                instant = duration_ms <= 0
                failed = any(row.data.get("error") for row in rows)
                parent = rows[0].parent
                attributes: list[dict[str, Any]] = [
                    {"key": "ai-coding.turn", "value": {"intValue": str(turn.turn)}},
                    {"key": "ai-coding.span", "value": {"stringValue": span_name}},
                    {"key": "ai-coding.events", "value": {"stringValue": "|".join(events)}},
                ]
                if scope:
                    attributes.append(
                        {"key": "ai-coding.scope", "value": {"stringValue": scope}}
                    )
                if instant:
                    attributes.append(
                        {"key": "ai-coding.instant", "value": {"boolValue": True}}
                    )
                for row in rows:
                    attributes.extend(_attributes(row.data))
                spans.append(
                    {
                        "traceId": _hash(f"{view.session}|{turn.turn}", _TRACE_ID_CHARS),
                        "spanId": _span_id(view.session, turn.turn, span_name, scope),
                        "parentSpanId": (
                            ""
                            if not parent
                            or (parent == "turn" and span_name == "turn" and not scope)
                            else _span_id(
                                view.session,
                                turn.turn,
                                parent,
                                _parent_scope(parent, scope, scopes_by_span),
                            )
                        ),
                        # A-13: the span name is one operation name, not the
                        # whole event log; the full list stays in
                        # ``ai-coding.events`` above.
                        "name": first_event,
                        "kind": 1,  # SPAN_KIND_INTERNAL
                        "startTimeUnixNano": str(start),
                        "endTimeUnixNano": str(
                            start + duration_ms * 1_000_000 if duration_ms else start + 1
                        ),
                        "attributes": attributes,
                        "status": {"code": 2 if failed else 0},
                    }
                )
        resources.append(
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {
                            "key": "ai-coding.session",
                            "value": {"stringValue": view.session},
                        },
                    ]
                },
                "scopeSpans": [
                    {"scope": {"name": "ai-coding.trace", "version": "1"}, "spans": spans}
                ],
            }
        )
    return {"resourceSpans": resources}
