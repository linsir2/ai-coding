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
    BUDGET = "budget"  # the turn's token ceiling was reached (R4b)
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
    #: The model's thinking for this step, if the endpoint produced any
    #: (R1-4/Project A).  Completed text per step — never streamed, never
    #: re-fed to the model (the request mapping ignores it).
    reasoning: str | None = None


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
    # Whether the turn's final word was cut by the model output ceiling
    # (``finish_reason="length"``).  A bare stop is indistinguishable from
    # done, so only the length cutoff is detectable — and it must surface
    # instead of reading as a completed answer.
    truncated_by_length: bool = False
    # R6a: the harness's own report for a turn that ended without a final word
    # (budget, clock or engine).  Deliberately *not* folded into ``text``: memory
    # reads ``text is None`` as "this turn concluded nothing", and a sub-agent's
    # conclusion is the model's own words (docs/r6-analysis.md 2.1, D-R6-4).
    handoff: str = ""


__all__ = ["TurnStatus", "StepResult", "TurnOutcome"]
