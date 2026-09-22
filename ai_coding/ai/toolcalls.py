"""Streaming tool-call fragment accumulation (R1a-2).

OpenAI-compatible endpoints deliver ``tool_calls`` as fragments keyed by
``index``: the first fragment carries ``id`` and ``function.name``, later ones
carry pieces of ``function.arguments``.  Fragments of different indices may
interleave in any order.

Rules (docs/r1a-2-analysis.md D3):
  - ``id``: first non-empty value wins (set-once) — a duplicated id is a real
    failure mode, whereas a split id is not.
  - ``name``: appended (identical to assignment for the usual single fragment,
    and still correct if a server splits it).
  - ``arguments``: appended in arrival order.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolCallRef


def _read(obj: Any, *path: str, default: Any = None) -> Any:
    """Read a nested value from a dict *or* an object, tolerating missing parts.

    The real SDK returns pydantic models; tests (and some clients) hand back
    plain dicts.  Supporting both keeps this module trivially testable.
    """
    current = obj
    for key in path:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return default if current is None else current


def merge_tool_call_delta(acc: dict[int, dict[str, str]], delta: Any) -> None:
    """Fold one streamed ``delta.tool_calls[i]`` into ``acc`` in place."""
    raw_index = _read(delta, "index")
    try:
        index = int(raw_index) if raw_index is not None else 0
    except (TypeError, ValueError):
        index = 0

    bucket = acc.setdefault(index, {"id": "", "name": "", "arguments": ""})

    call_id = _read(delta, "id")
    if call_id and not bucket["id"]:
        bucket["id"] = str(call_id)

    name = _read(delta, "function", "name")
    if name:
        bucket["name"] += str(name)

    arguments = _read(delta, "function", "arguments")
    if arguments:
        bucket["arguments"] += str(arguments)


def finalize_tool_calls(acc: dict[int, dict[str, str]]) -> list[ToolCallRef]:
    """Turn the accumulator into ordered ``ToolCallRef``s.

    Ordering is by fragment index; a missing ``id`` is synthesized and missing
    ``arguments`` become ``{}`` so downstream JSON parsing always succeeds.
    """
    calls: list[ToolCallRef] = []
    for index in sorted(acc):
        bucket = acc[index]
        calls.append(
            ToolCallRef(
                id=bucket.get("id") or f"call_{index}",
                name=bucket.get("name", ""),
                arguments=bucket.get("arguments") or "{}",
            )
        )
    return calls


__all__ = ["merge_tool_call_delta", "finalize_tool_calls"]
