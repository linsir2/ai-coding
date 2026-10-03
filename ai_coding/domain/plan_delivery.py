"""Step deliveries: what each plan-runner step left behind (R1-1).

A delivery is the persisted receipt of one executed plan step — which todo it
was, whether it passed its proof commands, and a one-line summary.  The
resume replay reads these to rebuild the step timeline; the messages alone
cannot do it (turn boundaries do not survive compaction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepDelivery:
    """One executed plan step, as the session remembers it."""

    step_id: str
    statement: str = ""
    #: completed | blocked | stalled
    status: str = "completed"
    #: How many bounded turns the step took (1, or 2 with a repair run).
    attempts: int = 1
    #: Acceptance verdicts observed: [{id, passed}].
    ac: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    #: (first, last) session turn numbers spanned.  Informational only —
    #: replay matches steps to messages by prompt marker, not by number.
    turns: tuple[int, int] = (0, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stepId": self.step_id,
            "statement": self.statement,
            "status": self.status,
            "attempts": self.attempts,
            "ac": [dict(entry) for entry in self.ac],
            "summary": self.summary,
            "turns": [int(self.turns[0]), int(self.turns[1])],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepDelivery:
        raw_ac = data.get("ac")
        ac = [dict(entry) for entry in raw_ac] if isinstance(raw_ac, list) else []
        raw_turns = data.get("turns") or [0, 0]
        turns = (int(raw_turns[0]), int(raw_turns[1])) if len(raw_turns) >= 2 else (0, 0)
        return cls(
            step_id=str(data.get("stepId", "")),
            statement=str(data.get("statement", "")),
            status=str(data.get("status", "completed")),
            attempts=int(data.get("attempts", 1) or 1),
            ac=ac,
            summary=str(data.get("summary", "")),
            turns=turns,
        )


__all__ = ["StepDelivery"]
