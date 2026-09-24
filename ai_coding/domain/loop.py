"""Loop-level contracts (R1a).

``StepResult`` is what one model call returns; ``TurnOutcome`` is how an entire
user turn ended.  Splitting them is what lets the orchestration layer own the
iteration loop (the engine only ever performs one step).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .message import ToolCallRef


class TurnStatus(enum.Enum):
    """How a turn terminated."""

    COMPLETED = "completed"  # model stopped and (if todo_aware) no open items
    MAX_ITERATIONS = "max_iterations"  # iteration budget exhausted
    TIMEOUT = "timeout"  # the wall-clock turn deadline cut the turn short (R2a)
    STOPPED = "stopped"  # approval callback asked to stop
    ABORTED = "aborted"  # user interrupted (CancelledError)
    ERROR = "error"  # engine failed


@dataclass(frozen=True)
class StepResult:
    """One model step: the text it emitted and the tools it asked for."""

    text: str | None = None
    tool_calls: list[ToolCallRef] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class TurnOutcome:
    """Terminal state of one ``process_input`` call."""

    status: TurnStatus
    text: str | None = None
    iterations: int = 0
    reason: str = ""
    # R4a: what the turn spent, summed over its model steps.  Carried on the
    # outcome so a caller does not have to go digging through a trace file to
    # answer "what did that cost?" (docs/r4-analysis.md 7.3).
    usage: dict[str, int] | None = None


__all__ = ["TurnStatus", "StepResult", "TurnOutcome"]
