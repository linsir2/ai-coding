"""Structured harvest contract (R3a).

The M3 extractor answered with one plain line per memory.  That cannot express
"this replaces that note", which is exactly what case 4 of docs/r3-analysis.md
needs (``uv`` -> ``poetry`` used to leave both notes alive and contradictory).

So the new contract is a JSON array of objects::

    [{"text": "...", "kind": "decision", "importance": 5,
      "action": "add|update|supersede|noop", "target_id": "<existing note id>"}]

Parsing is deliberately forgiving, because a model answer is untrusted input:
fenced code blocks, surrounding prose, junk rows and the old line-per-note format
are all accepted, and anything unusable degrades to an empty list (memory must
never raise — the M3 invariant survives).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ai_coding.memory.note import NoteKind

__all__ = ["ACTIONS", "HarvestCandidate", "parse_candidates"]

ACTIONS: frozenset[str] = frozenset({"add", "update", "supersede", "noop"})

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


@dataclass(frozen=True)
class HarvestCandidate:
    """One memory the extractor wants to write."""

    text: str
    kind: NoteKind = NoteKind.FACT
    importance: int = 3
    action: str = "add"
    target_id: str | None = None


def parse_candidates(raw: str) -> list[HarvestCandidate]:
    """Parse an extractor answer into candidates; never raises."""
    text = (raw or "").strip()
    if not text:
        return []
    for block in _possible_payloads(text):
        parsed = _try_json(block)
        if parsed is not None:
            return parsed
    return _from_lines(text)


def _possible_payloads(text: str) -> list[str]:
    payloads: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        payloads.append(fenced.group(1))
    payloads.append(text)
    array = _ARRAY.search(text)
    if array:
        payloads.append(array.group(0))
    return payloads


def _try_json(payload: str) -> list[HarvestCandidate] | None:
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    candidates: list[HarvestCandidate] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text", "") or "").strip()
        if not text:
            continue
        action = str(row.get("action", "add") or "add").strip().casefold()
        if action not in ACTIONS:
            action = "add"
        target = row.get("target_id")
        candidates.append(
            HarvestCandidate(
                text=text,
                kind=NoteKind.parse(row.get("kind", NoteKind.FACT.value)),
                importance=_clamp(row.get("importance", 3)),
                action=action,
                target_id=str(target).strip() if target else None,
            )
        )
    return candidates


def _from_lines(text: str) -> list[HarvestCandidate]:
    """Old-format fallback: one memory per non-empty line."""
    out: list[HarvestCandidate] = []
    for line in text.splitlines():
        body = line.strip().lstrip("-*•").strip()
        if not body:
            continue
        if body.startswith("{") or body.startswith("["):
            continue  # half-parsed JSON is not a memory
        out.append(HarvestCandidate(text=body))
    return out


def _clamp(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, number))
