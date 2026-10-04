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
    #: What the step's turns spent (summed model-step usage, R1-4) — the
    #: visibility half of the run budget.  ``None`` when the loop did not
    #: report it.
    usage: dict[str, int] | None = None
    #: Whether the runner itself settled this step in a bounded turn (R1-5).
    #: ``False`` marks a backfilled receipt: the model marked the item done
    #: outside any step turn, so there was no bounded run and no verify gate
    #: — the completion stands (done is done), the provenance is recorded.
    #: Defaults True: every receipt written before this field existed was
    #: runner-settled.
    attested: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "stepId": self.step_id,
            "statement": self.statement,
            "status": self.status,
            "attempts": self.attempts,
            "ac": [dict(entry) for entry in self.ac],
            "summary": self.summary,
            "turns": [int(self.turns[0]), int(self.turns[1])],
            "usage": dict(self.usage) if self.usage is not None else None,
            "attested": bool(self.attested),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepDelivery:
        raw_ac = data.get("ac")
        ac = [dict(entry) for entry in raw_ac] if isinstance(raw_ac, list) else []
        raw_turns = data.get("turns") or [0, 0]
        turns = (int(raw_turns[0]), int(raw_turns[1])) if len(raw_turns) >= 2 else (0, 0)
        raw_usage = data.get("usage")
        usage = (
            {str(key): int(value) for key, value in raw_usage.items()}
            if isinstance(raw_usage, dict)
            else None
        )
        return cls(
            step_id=str(data.get("stepId", "")),
            statement=str(data.get("statement", "")),
            status=str(data.get("status", "completed")),
            attempts=int(data.get("attempts", 1) or 1),
            ac=ac,
            summary=str(data.get("summary", "")),
            turns=turns,
            usage=usage,
            # Every receipt predating this field was runner-settled.
            attested=bool(data.get("attested", True)),
        )


__all__ = ["StepDelivery"]
