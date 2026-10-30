"""Sub-agent contracts (R1a-5).

A sub-agent runs the **same loop** as its parent, seeded with the parent turn's
request view and system prompt, and returns a structured packet rather than a
bare string.  Its internal reasoning is deliberately not returned
(docs/r1-analysis.md D10).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .message import ChatMessage, ToolArtifact, ToolCallRef

if TYPE_CHECKING:
    from ai_coding.security.permissions import Rule

#: ``SubagentResult.status`` values that mean "it did the work": a child that hit
#: its iteration ceiling still returns a usable conclusion, exactly like
#: ``ToolResult.success`` treats it.  One definition so the tool, the fan-out and
#: the plan executor cannot drift apart.
SUCCESS_STATUSES: frozenset[str] = frozenset({"completed", "max_iterations"})

#: Wire version of ``SubagentResult.to_dict()`` (the packet, and the ledger file
#: written from it).  Compatibility rule: **additive optional fields only**;
#: adding a required field, removing one, or changing a field's meaning bumps
#: this number.  v2 (r25) removed ``notes``/``confidence`` and added
#: ``worktree_branch``/``commit``.  Consumers read ``v`` before trusting the
#: shape.
SUBAGENT_PACKET_VERSION = 2

#: Caps for the structured brief.  One source for every internal caller: the
#: fan-out and any future plan-side research build the same object, and the
#: model-facing ``subagent`` tool keeps its free-form string (unchanged).
OBJECTIVE_MAX = 800
CONTEXT_MAX = 1_000
SCOPE_ITEMS_MAX = 10
SCOPE_ITEM_MAX = 200
CONSTRAINTS_MAX = 10
CONSTRAINT_MAX = 200
BRIEF_MAX = 4_000


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
    task block).  The system prompt is **not** inherited: a prompt is assembled
    for a specific tool surface and agent, so the child rebuilds its own
    (r25/v2; opencode's child session does the same).  Inheriting the assembled
    text appended project/skills/methods a second time and advertised tools the
    child does not have.
    """

    messages: tuple[ChatMessage, ...] = ()
    #: M1a ④: the parent's session rule layer.  A child has a fresh session,
    #: so without this its gate would see the files and the config but not
    #: what the operator denied in the conversation that spawned it.
    permissions: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class SubagentBrief:
    """The frozen parent → child brief used by internal callers.

    The model-facing ``subagent`` tool keeps its free-form ``task`` string on
    purpose (the model writes its own delegation); this object is what our own
    code produces, so the child sees one deterministic envelope instead of an
    ad-hoc paragraph per call site.

    ``problems()`` is a refusal list, never a clamp: a caller that cannot fit
    the caps must shorten the brief itself (silent truncation loses exactly the
    fact the child needed).  ``render()`` is deterministic and pinned by tests.
    """

    objective: str
    scope: tuple[str, ...] = ()
    context: str = ""
    constraints: tuple[str, ...] = ()

    def problems(self) -> tuple[str, ...]:
        """Everything that makes this brief unfit to send (empty = valid)."""
        issues: list[str] = []
        objective = self.objective.strip()
        if not objective:
            issues.append("objective is required")
        elif len(objective) > OBJECTIVE_MAX:
            issues.append(
                f"objective is longer than {OBJECTIVE_MAX} characters"
            )
        if len(self.context.strip()) > CONTEXT_MAX:
            issues.append(f"context is longer than {CONTEXT_MAX} characters")
        issues.extend(
            _list_problems("scope", self.scope, SCOPE_ITEMS_MAX, SCOPE_ITEM_MAX)
        )
        issues.extend(
            _list_problems(
                "constraints", self.constraints, CONSTRAINTS_MAX, CONSTRAINT_MAX
            )
        )
        rendered = self.render()
        if len(rendered) > BRIEF_MAX:
            issues.append(f"brief is longer than {BRIEF_MAX} characters")
        return tuple(issues)

    def render(self) -> str:
        """The child's task text: a fixed envelope, sections omitted when empty."""
        lines = ["[TASK]", self.objective.strip()]
        context = self.context.strip()
        if context:
            lines += ["", "[CONTEXT]", context]
        if self.scope:
            lines += ["", "[SCOPE]", *(f"- {item.strip()}" for item in self.scope)]
        if self.constraints:
            lines += [
                "",
                "[CONSTRAINTS]",
                *(f"- {item.strip()}" for item in self.constraints),
            ]
        return "\n".join(lines)


def _list_problems(
    name: str, values: tuple[str, ...], max_items: int, max_chars: int
) -> list[str]:
    issues: list[str] = []
    if len(values) > max_items:
        issues.append(f"{name} has more than {max_items} items")
    for index, value in enumerate(values):
        text = value.strip()
        if not text:
            issues.append(f"{name} item {index + 1} is empty")
        elif len(text) > max_chars:
            issues.append(
                f"{name} item {index + 1} is longer than {max_chars} characters"
            )
    return issues


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
    #: r25/v2: the branch a worktree-isolated child committed its changes to,
    #: and the commit sha.  Without these the packet advertised ``artifacts``
    #: paths inside a worktree that is force-removed on return — a write
    #: delegation looked successful while the work was gone.
    worktree_branch: str | None = None
    commit: str | None = None

    @property
    def text(self) -> str | None:
        """Backwards-compatible alias for :attr:`conclusion` (M4 call sites)."""
        return self.conclusion

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SUBAGENT_PACKET_VERSION,
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
            "worktree_branch": self.worktree_branch,
            "commit": self.commit,
        }


def extract_json_facts(
    text: str | None, schema: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, bool, tuple[str, ...]]:
    """Pull a JSON object out of ``text`` and check it against ``schema``.

    Forgiving about *shape* (code fences stripped, prose around the object
    tolerated), strict about *content*: the ``required`` key list and every
    declared ``type``/``items.type`` are enforced — every other JSON Schema
    keyword is ignored by design (a subset interpreter that never pretends to
    understand ``oneOf``/``pattern``/``$ref``).  Extra keys are tolerated,
    values are never coerced: a wrong type is reported, not silently fixed.

    A wrong type on a **required** key invalidates the packet (no facts).  A
    wrong type on an **optional** declared key drops just that key and keeps
    the rest: one malformed optional field must not discard a good summary.

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
    # Declared types are checked, undeclared keys stay "presence only".  This is
    # still not a JSON Schema interpreter (no oneOf/$ref/pattern), but a schema
    # that says ``summary: string`` must not accept ``{"summary": 123}`` -- the
    # parent would otherwise get a number where its consumer expects prose.
    if not isinstance(schema, dict):
        return dict(parsed), True, ()
    fatal, optional, dropped = _type_problems(parsed, schema, set(required))
    if fatal:
        return None, False, tuple(fatal + optional)
    kept = {key: value for key, value in parsed.items() if key not in dropped}
    if optional:
        return kept, False, tuple(optional)
    return kept, True, ()


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_problems(
    data: dict[str, Any], schema: dict[str, Any], required: set[str]
) -> tuple[list[str], list[str], set[str]]:
    """``(fatal, optional, dropped)`` type mismatches for declared properties.

    A required key's wrong type is fatal (the caller returns no facts); an
    optional key's wrong type is dropped and reported, so a good required
    payload survives one malformed optional field.
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return [], [], set()
    fatal: list[str] = []
    optional: list[str] = []
    dropped: set[str] = set()

    def note(key: str, message: str) -> None:
        if key in required:
            fatal.append(message)
        else:
            optional.append(message)
            dropped.add(key)

    for key, spec in properties.items():
        if key not in data or not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if not isinstance(expected, str) or expected not in _TYPES:
            continue
        value = data[key]
        if expected in ("integer", "number") and isinstance(value, bool):
            note(key, f"key {key!r} must be {expected}, got bool")
            continue
        if not isinstance(value, _TYPES[expected]):
            note(key, f"key {key!r} must be {expected}, got {type(value).__name__}")
            continue
        if expected != "array" or not isinstance(value, list):
            continue
        items = spec.get("items")
        item_type = items.get("type") if isinstance(items, dict) else None
        if not isinstance(item_type, str) or item_type not in _TYPES:
            continue
        for index, entry in enumerate(value):
            if item_type in ("integer", "number") and isinstance(entry, bool):
                note(
                    key,
                    f"key {key!r} items must be {item_type}; index {index} is bool",
                )
                break
            if not isinstance(entry, _TYPES[item_type]):
                note(
                    key,
                    f"key {key!r} items must be {item_type}; "
                    f"index {index} is {type(entry).__name__}",
                )
                break
    return fatal, optional, dropped


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
