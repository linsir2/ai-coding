"""Sub-agent contracts (R1a-5).

A sub-agent runs the **same loop** as its parent, seeded with the parent turn's
request view and system prompt, and returns a structured packet rather than a
bare string.  Its internal reasoning is deliberately not returned
(docs/r1-analysis.md D10).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .message import ChatMessage, ToolArtifact, ToolCallRef


@dataclass(frozen=True)
class SubagentTurn:
    """A single sub-agent turn: an optional conclusion and pending tool calls."""

    text: str = ""
    tool_calls: list[ToolCallRef] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class ParentContext:
    """What the parent turn was looking at when it delegated.

    ``messages`` is the parent request view (compressed, *before* its volatile
    task block) and ``instructions`` is the assembled system prompt — so the
    sub-agent sees exactly what the parent saw on that step.
    """

    messages: tuple[ChatMessage, ...] = ()
    instructions: str | None = None


@dataclass(frozen=True)
class SubagentResult:
    """The packet a sub-agent returns to its caller."""

    status: str = "completed"  # completed | max_iterations | timeout | error | cancelled
    conclusion: str | None = None
    todos: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[ToolArtifact, ...] = ()
    iterations: int = 0
    workdir: str | None = None
    ledger_path: str | None = None
    error: str | None = None
    #: Which window the child ran with (R7e-4): ``full`` or ``task_only``.
    context_mode: str = "full"
    #: R0-3: the child has more to say/do but was cut short (budget ceiling,
    #: timeout, or halt) — unfinished, with a cause below.  Without this a
    #: budget stop reads like a result: C1 recorded a timeout the parent
    #: could only detect by parsing prose.
    truncated: bool = False
    #: R0-3: machine-readable why-it-stopped (e.g. "outer_timeout",
    #: "turn deadline exceeded: ...").  ``status`` says the class, this says
    #: the cause.
    exit_reason: str | None = None
    #: R0-3: what the child spent (the parent's cost accounting never saw
    #: children before — no scoped events reached it and the ledger file had
    #: no usage).  Summed model-step usage, ``None`` when unknown.
    usage: dict[str, int] | None = None
    #: R1-3: the structured payload parsed out of ``conclusion`` when the
    #: caller passed an ``output_schema`` — machine facts the parent can read
    #: without parsing prose (C1's parent hand-copied "25/25" out of a
    #: paragraph).  ``None`` when no schema was requested, or when parsing
    #: failed (the raw conclusion always survives — see ``schema_valid``).
    facts: dict[str, Any] | None = None
    #: R1-3: tri-state on purpose — ``None`` means no schema was requested
    #: (nothing to validate), ``True``/``False`` is the verdict.  A ``False``
    #: never sinks the packet: the work stands, only the shape failed.
    schema_valid: bool | None = None
    #: R1-3: why validation failed (missing keys, bad JSON) — named so the
    #: parent can ask for exactly what is absent instead of guessing.
    schema_errors: tuple[str, ...] = ()
    #: F4: notes for future sub-agents (trace field).
    notes: str = ""
    #: F4: confidence level (high/medium/low) — uncertainty protocol.
    confidence: str = "high"

    @property
    def text(self) -> str | None:
        """Backwards-compatible alias for :attr:`conclusion` (M4 call sites)."""
        return self.conclusion

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "conclusion": self.conclusion,
            "todos": [dict(entry) for entry in self.todos],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "iterations": self.iterations,
            "workdir": self.workdir,
            "ledger_path": self.ledger_path,
            "error": self.error,
            "context_mode": self.context_mode,
            "truncated": self.truncated,
            "exit_reason": self.exit_reason,
            "usage": dict(self.usage) if self.usage is not None else None,
            "facts": dict(self.facts) if self.facts is not None else None,
            "schema_valid": self.schema_valid,
            "schema_errors": list(self.schema_errors),
            "notes": self.notes,
            "confidence": self.confidence,
        }


def extract_json_facts(
    text: str | None, schema: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, bool, tuple[str, ...]]:
    """Pull a JSON object out of ``text`` and check it against ``schema``.

    Forgiving about *shape* (code fences stripped, prose around the object
    tolerated), strict about *content*: only the ``required`` key list is
    enforced — every other JSON Schema keyword is ignored by design (a subset
    interpreter that never pretends to understand ``oneOf``/``pattern``/
    ``$ref``).  Extra keys are tolerated, values are never coerced: a wrong
    type is reported, not silently fixed.

    Returns ``(facts, valid, errors)``.  Callers pass a non-empty schema;
    an empty/missing schema means "no contract requested", not "anything
    goes" — that tri-state lives on :attr:`SubagentResult.schema_valid`.
    """
    required = schema.get("required", []) if isinstance(schema, dict) else []
    if not isinstance(required, list):
        required = []
    if not text or not text.strip():
        return None, False, ("empty conclusion: nothing to parse",)
    candidate = _strip_fences(text.strip())
    parsed: Any = None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        for block in _object_blocks(candidate):
            try:
                parsed = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            break
        if parsed is None:
            return None, False, ("no JSON object found in conclusion",)
    if not isinstance(parsed, dict):
        return None, False, ("top-level JSON must be an object",)
    missing = [key for key in required if key not in parsed]
    if missing:
        return None, False, (f"missing required keys: {', '.join(map(str, missing))}",)
    return dict(parsed), True, ()


def _strip_fences(text: str) -> str:
    """Drop one wrapping markdown fence (``` or ```json ... ```)."""
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    body = lines[1:] if len(lines) > 1 else []
    if body and body[-1].strip() == "```":
        body = body[:-1]
    return "\n".join(body).strip()


def _object_blocks(text: str) -> list[str]:
    """Candidate ``{...}`` spans, first-outermost first, string-aware."""
    blocks: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escaped = False
        for j in range(i, len(text)):
            char = text[j]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[i : j + 1])
                    break
        i += 1
    return blocks


__all__ = [
    "BackgroundTask",
    "SubagentTurn",
    "ParentContext",
    "SubagentResult",
    "extract_json_facts",
]


#: Terminal states a background task can rest in.  ``running`` is the only
#: live one; ``rejected`` means it never started (pool saturated at spawn).
BACKGROUND_FINAL = frozenset({"completed", "timeout", "error", "cancelled", "rejected"})


@dataclass(frozen=True)
class BackgroundTask:
    """One delegated run tracked across turns (R1-2).

    The table lives in the runner's memory — a restart loses it (same rule
    as Hermes: a running child cannot be proven side-effect-free, so it is
    never resumed).  Times are ``time.monotonic`` seconds for elapsed math.
    """

    run_id: str
    name: str = "sub-agent"
    task: str = ""
    status: str = "running"  # running | completed | timeout | error | cancelled | rejected
    started: float = 0.0
    ended: float | None = None
    error: str | None = None
    usage: dict[str, int] | None = None
    #: C-3b: where the settled run's full packet was written, so a listing can
    #: hand the model a pointer the ``read`` tool can follow.  None while the
    #: run is live, and for rows that never wrote one (rejected at spawn).
    ledger_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "name": self.name,
            "task": self.task,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "error": self.error,
            "usage": dict(self.usage) if self.usage is not None else None,
            "ledgerPath": self.ledger_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackgroundTask:
        usage = data.get("usage")
        return cls(
            run_id=str(data.get("runId", "")),
            name=str(data.get("name", "sub-agent")),
            task=str(data.get("task", "")),
            status=str(data.get("status", "running")),
            started=float(data.get("started", 0.0) or 0.0),
            ended=data.get("ended"),
            error=data.get("error"),
            usage=dict(usage) if isinstance(usage, dict) else None,
            ledger_path=(
                str(data["ledgerPath"]) if data.get("ledgerPath") is not None else None
            ),
        )
