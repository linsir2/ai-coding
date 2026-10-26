"""M-C1: the write-ahead contract for side-effecting tool calls.

A ``mutates_world`` call is recorded in a sidecar *before* it runs and settled
*after* it returns, so a call the model regenerates after a crash can be
answered from the record instead of running a second time.  The record is the
claim OpenCode writes before it wakes the session and the idempotency key the
recovery literature lands on: ``(tool, digest)`` names the *intent*, so a
regenerated call matches the one that already ran.

The three caps exist because an unbounded record per call is how a sidecar
becomes a second transcript.  ``output`` is the biggest: a crash's tool message
never reached the file (``resume_turn``'s history stops at the last completed
result), so on resume the model sees *only* what is recorded here -- 200 chars
was a real information loss for a large output.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: The record's caps (M-C1).  Every one is a bound: a sidecar is crash recovery,
#: not a second transcript.
OUTPUT_CHARS = 4000
DATA_KEYS = 8
DATA_VALUE_CHARS = 200
ARTIFACTS = 4

__all__ = [
    "ARTIFACTS",
    "DATA_KEYS",
    "DATA_VALUE_CHARS",
    "OUTPUT_CHARS",
    "WriteAheadIntent",
    "clip_artifacts",
    "clip_data",
    "clip_output",
]


def clip_output(text: Any) -> str:
    """Cap the recorded output; ``None`` reads as the empty string."""
    return ("" if text is None else str(text))[:OUTPUT_CHARS]


def clip_data(data: Any) -> dict[str, Any]:
    """Cap the recorded structured result by key count and per-value size.

    Strings are clipped in place; containers are clipped recursively (depth and
    breadth bounded); anything else is kept as-is (a scalar) or replaced by its
    ``repr`` (an object JSON cannot encode).  The cap is on the value's *size*,
    not on ``str`` alone: a tool whose ``data`` is a list or a nested dict
    (``grep``/``glob`` shape one) would otherwise pass through whole, and the
    sidecar would grow with the tool's output rather than with the call count.
    """
    if not isinstance(data, dict):
        return {}
    return {
        str(key): _clip_value(value) for key, value in list(data.items())[:DATA_KEYS]
    }


def _clip_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value[:DATA_VALUE_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict) and depth < 2:
        return {
            str(key): _clip_value(item, depth + 1)
            for key, item in list(value.items())[:DATA_KEYS]
        }
    if isinstance(value, list) and depth < 2:
        return [_clip_value(item, depth + 1) for item in value[:DATA_KEYS]]
    return repr(value)[:DATA_VALUE_CHARS]


def clip_artifacts(artifacts: Iterable[Any]) -> list[dict[str, Any]]:
    """Cap the recorded side effects, each as its wire dict."""
    rows: list[dict[str, Any]] = []
    for item in list(artifacts or ())[:ARTIFACTS]:
        to_dict = getattr(item, "to_dict", None)
        row = to_dict() if callable(to_dict) else {"path": str(item)}
        rows.append(dict(row) if isinstance(row, dict) else {"path": str(item)})
    return rows


@dataclass
class WriteAheadIntent:
    """One side-effecting tool call, recorded before it runs (M-C1).

    ``running`` is deliberately *not* a hit: the side effect may or may not have
    happened, so recovery re-runs it.  Only ``ok`` is reused.  Closing the
    remaining window (effect done, ``ok`` not yet on disk) needs an outbox and
    fencing tokens, which this repo does not have (§七 of the plan).

    ``output``/``data``/``artifacts`` are carried because a reuse has to be
    *indistinguishable* from a fresh run: the model reads ``output``, and the
    turn's digest/handoff read ``artifacts`` (``turn_close.py``).  Dropping them
    would silently shrink the turn's evidence.
    """

    call_id: str
    tool: str
    #: ``call_fingerprint(call)`` -- reused from ``core/guard.py``, not reinvented.
    digest: str
    #: "running" | "ok" | "failed"
    status: str = "running"
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "callId": str(self.call_id),
            "tool": str(self.tool),
            "digest": str(self.digest),
            "status": str(self.status),
            "output": str(self.output),
            "data": dict(self.data),
            "artifacts": [dict(row) for row in self.artifacts],
        }

    @classmethod
    def from_dict(cls, data: Any) -> WriteAheadIntent | None:
        if not isinstance(data, dict):
            return None
        digest = data.get("digest")
        tool = data.get("tool")
        if not isinstance(digest, str) or not digest:
            return None
        if not isinstance(tool, str) or not tool:
            return None
        status = str(data.get("status", "running") or "running")
        if status not in ("running", "ok", "failed"):
            status = "running"
        raw_data = data.get("data")
        raw_artifacts = data.get("artifacts")
        return cls(
            call_id=str(data.get("callId", "") or ""),
            tool=tool,
            digest=digest,
            status=status,
            output=str(data.get("output", "") or ""),
            data=dict(raw_data) if isinstance(raw_data, dict) else {},
            artifacts=[
                dict(row)
                for row in (raw_artifacts if isinstance(raw_artifacts, list) else [])
                if isinstance(row, dict)
            ],
        )
