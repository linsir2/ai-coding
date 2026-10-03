"""Acceptance contracts: done-ness you verify, not claim (R0-1).

An acceptance item is one machine-checkable statement plus the shell command
that checks it (``verify`` runs in the workspace, same trust level as the
``bash`` tool — these items are user-authored session content).  The loop
runs the command (bounded, ``core/acceptance.py``); the policy only ever
sees the result, so ``TurnPolicy`` stays pure.

Population is programmatic for now (``session.acceptance = [...]``); the
plan runner (R1-1) will produce these from plans.  What this layer delivers
is verification, gating and persistence — no model parsing of free text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Evidence tail kept on an item: a full pytest log must never live in the
#: session file (R1a-3 carries previews only, for the same reason).
_OUTPUT_TAIL = 500


@dataclass
class AcceptanceItem:
    """One verifiable statement: what is true, and the command proving it."""

    id: str
    statement: str = ""
    verify: str = ""
    #: None = not checked yet; fail-closed everywhere it is read.
    passed: bool | None = None
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "verify": self.verify,
            "passed": self.passed,
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AcceptanceItem:
        output = str(data.get("output", ""))
        return cls(
            id=str(data.get("id", "")),
            statement=str(data.get("statement", "")),
            verify=str(data.get("verify", "")),
            passed=data.get("passed"),
            output=output[-_OUTPUT_TAIL:] if len(output) > _OUTPUT_TAIL else output,
        )


@dataclass(frozen=True)
class AcceptanceState:
    """The policy-visible shadow of an item: id, verdict, and evidence tail.

    The tail travels with the verdict so a nudge can quote *why* a check
    failed — without it the model would re-run the suite blind, and the extra
    subprocess calls would be pure wall-clock burn (measured).
    """

    id: str
    passed: bool | None = None
    output: str = ""


def acceptance_states(items: list[AcceptanceItem]) -> tuple[AcceptanceState, ...]:
    """Project session items to what ``TurnSnapshot`` carries."""
    return tuple(
        AcceptanceState(id=item.id, passed=item.passed, output=item.output)
        for item in items
    )


def pending_items(items: list[AcceptanceItem]) -> list[AcceptanceItem]:
    """Items still needing a check: failed or never checked."""
    return [item for item in items if item.passed is not True]


def failed_items(items: list[AcceptanceItem]) -> list[AcceptanceItem]:
    """Items checked and failing.  Unchecked items fail closed at the gate."""
    return [item for item in items if item.passed is False]


__all__ = [
    "AcceptanceItem",
    "AcceptanceState",
    "acceptance_states",
    "failed_items",
    "pending_items",
]
