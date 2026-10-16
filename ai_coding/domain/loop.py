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


class EmptyStepError(Exception):
    """A model step that thought but neither spoke nor acted (A6).

    ``ChatMessage`` refuses such a step on purpose — an assistant message with
    neither content nor tool_calls is rejected by providers on the next request
    — so the loop has to decide what the turn does instead.  Raising this lets
    it end as an ordinary turn failure with a reason a person can read, rather
    than letting the domain error escape to the IPC catch-all where the turn
    loses its bookkeeping (sessions, iteration count) and the screen shows a
    Python message.

    ``finish_reason`` is the provider's own verdict (``"stop"`` and friends).
    It travels with the error because it is what tells an operator whether this
    was the upstream declining to answer or something we did.
    """

    def __init__(self, finish_reason: str | None = None) -> None:
        super().__init__(f"empty model step (finish_reason={finish_reason or 'unknown'})")
        self.finish_reason = finish_reason


class ThinkingOnlyStepError(Exception):
    """A step that carried reasoning and nothing else (M7).

    A reasoning model can spend its whole budget thinking and stop with nothing
    to show for it.  That is not the same as saying nothing at all: the
    thinking was emitted and is on the operator's screen, so the turn did get
    an answer — in the register this provider thinks in.  It is kept apart from
    :class:`EmptyStepError` precisely so the verdict can say so in words.

    ``reasoning_chars`` is not decoration either: it is what separates "the
    model gave up immediately" from "the model thought at length and then ran
    out", which are different advice for whoever reads the turn.
    """

    def __init__(self, finish_reason: str | None = None, reasoning_chars: int = 0) -> None:
        super().__init__(
            f"thinking-only model step (finish_reason={finish_reason or 'unknown'},"
            f" reasoning={reasoning_chars} chars)"
        )
        self.finish_reason = finish_reason
        self.reasoning_chars = reasoning_chars


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
    # stage 4 r2: the harness's own line for the operator when a plan-posture
    # turn ends without a submission -- same shape as ``handoff`` (a status,
    # not an answer); the transcript draws it as a muted notice.
    note: str = ""


__all__ = ["EmptyStepError", "ThinkingOnlyStepError", "TurnStatus", "StepResult", "TurnOutcome"]
