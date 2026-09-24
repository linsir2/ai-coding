"""What a turn tells the world (R5).

Events, token accounting and degradation reports used to be scattered through
`agent_loop`: two emit helpers, six inline ``Degraded(...)`` constructions, a
ledger, and an observer-failure counter — plus two *more* private copies of the
same degradation idea in `mcp` and `subagent`.

This module is the single home for that.  It is deliberately **write-only**:
it never changes the request, the transcript or a decision, so a turn's behaviour
is identical whether anyone is listening or not (docs/r4-analysis.md P1).

Two pieces:

* :func:`report_degraded` — a free function, because anything holding an observer
  may need to say "this enhancement fell back" (the MCP manager, a sub-agent).
* :class:`TurnReporter` — the per-turn reporter the loop drives.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace

from ai_coding.core.budget import BudgetLedger
from ai_coding.core.context_compression import estimate_tokens
from ai_coding.core.events import (
    ContextAssembled,
    Degraded,
    LoopEvent,
    LoopObserver,
    ModelStepFinished,
    TurnFinished,
)
from ai_coding.core.trace import canonical_request, digest_of, prefix_shared
from ai_coding.domain.loop import StepResult, TurnOutcome
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.usage import cache_from_usage

__all__ = ["TurnReporter", "report_degraded"]

_log = logging.getLogger(__name__)


def report_degraded(
    observer: LoopObserver | None,
    component: str,
    reason: str,
    fallback: str = "",
) -> None:
    """Say that an enhancement gave up and fell back.

    Best-effort by construction: observation must never be able to break the
    thing it observes, so a missing or exploding observer is swallowed here
    (docs/r5-analysis.md 1.3 — one implementation instead of three).
    """
    if observer is None:
        return
    try:
        observer.on_event(
            Degraded(component=component, reason=reason, fallback=fallback)
        )
    except Exception:  # noqa: BLE001 — reporting must never break a run
        _log.debug("degraded report failed for %s", component, exc_info=True)


class TurnReporter:
    """One turn's outward account: events, ledger, degradation."""

    def __init__(
        self,
        *,
        tools_text: str,
        emit: Callable[[LoopEvent], None],
    ) -> None:
        self._tools_text = tools_text
        self._tools_chars = len(tools_text)
        self._emit = emit
        self._ledger = BudgetLedger()
        self._failures_before = 0

    # ---- facts a caller needs ---------------------------------------------

    @property
    def tools_chars(self) -> int:
        """Serialised size of the tool schemas — part of every prompt."""
        return self._tools_chars

    @property
    def ledger(self) -> BudgetLedger:
        """The turn's ledger, so guards and the stop policy can read the spend."""
        return self._ledger

    def tokens_spent(self) -> int:
        return self._ledger.totals.total_tokens

    # ---- per turn ----------------------------------------------------------

    def begin_turn(self, observer: LoopObserver) -> None:
        """Remember the observer's failure count so the turn can report its own."""
        self._failures_before = int(getattr(observer, "failures", 0) or 0)

    def report_context(
        self,
        *,
        iteration: int,
        instructions: str | None,
        history: Sequence[ChatMessage],
        request: Sequence[ChatMessage],
        tail_blocks: tuple[tuple[str, int], ...],
        previous: str | None,
    ) -> str:
        """Report how this request was assembled; return its canonical form.

        The return value is what the next iteration compares against to measure
        the shared prefix, so the caller never has to rebuild it.
        """
        canonical = canonical_request(instructions, request, self._tools_text)
        shared_chars, shared_ratio = prefix_shared(previous, canonical)
        event = ContextAssembled(
            iteration=iteration,
            system_chars=len(instructions or ""),
            tools_chars=self._tools_chars,
            history_messages=len(history),
            history_chars=sum(len(message.content or "") for message in history),
            tail_blocks=tail_blocks,
            estimated_tokens=estimate_tokens(canonical),
            prefix_shared_chars=shared_chars,
            prefix_shared_ratio=shared_ratio,
            digest=digest_of(canonical),
        )
        self._ledger.record_assembly(event.estimated_tokens, shared_ratio)
        self._emit(event)
        return canonical

    def report_step(self, *, index: int, step: StepResult, latency_ms: int) -> None:
        """Report one model step's real cost, and feed the ledger."""
        cached, cache_write = cache_from_usage(step.usage)
        self._emit(
            ModelStepFinished(
                index=index,
                finish_reason=step.finish_reason,
                text_chars=len(step.text or ""),
                tool_call_count=len(step.tool_calls),
                usage=step.usage,
                cached_tokens=cached,
                cache_write_tokens=cache_write,
                latency_ms=latency_ms,
            )
        )
        self._ledger.record_step(
            step.usage, cached=cached, cache_write=cache_write, latency_ms=latency_ms
        )

    def report_degraded(self, component: str, reason: str, fallback: str = "") -> None:
        """Report a fallback through the turn's own emit path."""
        self._emit(Degraded(component=component, reason=reason, fallback=fallback))

    def finish_turn(self, outcome: TurnOutcome, observer: LoopObserver) -> TurnOutcome:
        """Close the turn: attach the spend, note a broken observer, emit the end.

        Order is deliberate — the observer line belongs to this turn's trace, so
        it is emitted before ``TurnFinished`` closes the file
        (docs/r4-analysis.md 4.2).
        """
        outcome = replace(outcome, usage=self._ledger.usage_dict())
        if int(getattr(observer, "failures", 0) or 0) > self._failures_before:
            self.report_degraded(
                "observer",
                "an observer raised while rendering",
                "the turn continued without it",
            )
        self._emit(TurnFinished(outcome=outcome))
        return outcome

    # ---- introspection (used by tests and by the loop's own bookkeeping) ----

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TurnReporter(tools_chars={self._tools_chars}, steps={self._ledger.totals.steps})"
