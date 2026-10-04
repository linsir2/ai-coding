"""What a turn leaves behind: the transcript, the handoff, the digest (R7a).

The loop's tail used to be five private methods (``_write_tool_messages``,
``_checkpoint``, ``_finalize``, ``_save``, ``_close_turn``) plus four module-level
factories that turned "stop, but why" into a :class:`TurnOutcome`.  They all answer
one question — *what does this turn leave on disk, and what does it say if the
model never spoke?* — so they live here, and the loop keeps only the choreography.

Two invariants from R6a survive verbatim and are the reason this file has tests:

* ``TurnOutcome.text`` is **never** rewritten.  The handoff is harness-authored, so
  callers must still be able to tell whether the model itself delivered
  (docs/r6-analysis.md 2.1, D-R6-4);
* the **digest** is appended on every close (the ring is what the next request's
  ``<working-memory>`` block renders from) while the **handoff** appears only when
  there is no text to show.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from ai_coding.core.dispatch import DispatchOutcome
from ai_coding.core.turn_policy import TurnDecision
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolArtifact, ToolOutput
from ai_coding.domain.run import tool_message
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import append_digest, build_digest, render_handoff
from ai_coding.infra.deadline import DeadlineExpired
from ai_coding.infra.time_utils import now_utc

__all__ = [
    "LONG_SESSION_STRIDE",
    "TurnCloser",
    "aborted_outcome",
    "failed_outcome",
    "stopped_outcome",
    "terminal_outcome",
]

_log = logging.getLogger(__name__)

#: Once a session is longer than the persist limit, write every Nth iteration.
LONG_SESSION_STRIDE = 3


# ---- "stop, but why" -------------------------------------------------------


def terminal_outcome(
    decision: TurnDecision,
    *,
    budget_expired: bool,
    last_text: str | None,
    iterations: int,
) -> TurnOutcome:
    """Translate "stop, but why" into the turn's terminal state (R2/R4b)."""
    if budget_expired:
        status = TurnStatus.TIMEOUT
    elif decision.reason == "turn token budget exhausted":
        status = TurnStatus.BUDGET
    else:
        status = TurnStatus.MAX_ITERATIONS
    return TurnOutcome(
        status=status, text=last_text, iterations=iterations, reason=decision.reason
    )


def stopped_outcome(last_text: str | None, iterations: int) -> TurnOutcome:
    """The approval callback asked to stop; the turn ends where it stood."""
    return TurnOutcome(
        status=TurnStatus.STOPPED,
        text=last_text,
        iterations=iterations,
        reason="user stopped the turn",
    )


def aborted_outcome(last_text: str | None, iterations: int) -> TurnOutcome:
    """The user interrupted the turn; the partial work is still worth keeping."""
    return TurnOutcome(
        status=TurnStatus.ABORTED, text=last_text, iterations=iterations, reason="cancelled"
    )


def failed_outcome(
    error: Exception, *, last_text: str | None, iterations: int
) -> TurnOutcome:
    """A deadline or an engine failure — the two states that end a turn early."""
    if isinstance(error, DeadlineExpired):
        reason = (
            f"turn deadline exceeded: {error.label} was abandoned after "
            f"{error.timeout:g}s"
        )
        return TurnOutcome(
            status=TurnStatus.TIMEOUT, text=last_text, iterations=iterations, reason=reason
        )
    return TurnOutcome(
        status=TurnStatus.ERROR, text=None, iterations=iterations, reason=str(error)
    )


# ---- the closing half ------------------------------------------------------


class TurnCloser:
    """Persistence, write-back and closing for one loop.

    The loop builds one of these; every turn drives it.  Failures here are
    *reported and degraded*, never raised: losing the session file must not lose
    the answer the user already paid for (docs/r4-analysis.md 1.1).
    """

    def __init__(self, sessions: Any, reporter: Any, settings: Any) -> None:
        self._sessions = sessions
        self._reporter = reporter
        self._settings = settings
        #: Artifacts this turn produced (feeds the digest and the handoff).
        self.artifacts: list[ToolArtifact] = []
        #: The last closed outcome (the loop exposes it as ``last_outcome``).
        self.last_outcome: TurnOutcome | None = None

    # ---- during the turn ---------------------------------------------------

    def begin_turn(self) -> None:
        """Forget the previous turn's artifacts."""
        self.artifacts = []

    def write_back(
        self, session: SessionData, outcomes: Sequence[DispatchOutcome]
    ) -> None:
        """Append one tool message per outcome (the model's own words stay put)."""
        for item in outcomes:
            self.artifacts.extend(item.result.artifacts)
            session.add_message(
                tool_message(
                    ToolOutput(
                        call_id=item.call.id,
                        tool_name=item.call.name,
                        output=item.result.output,
                        success=item.result.success,
                        data=item.result.data,
                        artifacts=item.result.artifacts,
                    ),
                    now_utc(),
                )
            )

    def checkpoint(self, session: SessionData, index: int) -> None:
        """Persist mid-turn, throttled once the session has grown large."""
        limit = int(self._settings.persist_checkpoint_messages)
        if len(session.messages) <= limit or index % LONG_SESSION_STRIDE == 0:
            self.save(session)

    def save(self, session: SessionData) -> None:
        """Persist the session, reporting (not raising) a failure.

        Internal sessions (``ephemeral``) are skipped: they run on the main
        loop, so without this guard every plan step would leave a
        "plan step N" file in the sessions directory and the /resume picker
        would list them (H1 audit fix).
        """
        if getattr(session, "ephemeral", False):
            return
        session.ensure_tool_pairing()
        try:
            self._sessions.save(session)
        except OSError:
            # Persistence is best-effort, but a silent failure used to be
            # invisible; R4a at least says so (docs/r4-analysis.md 1.1).
            _log.debug("session save failed", exc_info=True)
            self._reporter.report_degraded(
                "persist",
                "the session file could not be written",
                "the transcript stays in memory only",
            )

    # ---- closing -----------------------------------------------------------

    def finalize(self, session: SessionData, outcome: TurnOutcome, observer: Any) -> None:
        """Persist, then close the turn through the reporter (order matters)."""
        self.save(session)
        self.last_outcome = self._reporter.finish_turn(outcome, observer)

    def close(
        self,
        session: SessionData,
        *,
        user_input: str,
        task: TaskState,
        turn_number: int,
        outcome: TurnOutcome,
        failures: Sequence[tuple[str, str, str]] = (),
        trace_path: str = "",
        observer: Any = None,
        model_note: str = "",
    ) -> TurnOutcome:
        """Record what this turn leaves behind, then persist it (R6a).

        Two things happen here, in this order, because both have to be in the saved
        session: the **handoff** (what to tell the user when the model ran out of
        turn without a final word) and the **digest** (the one line the next
        request's ``<working-memory>`` block is rendered from).
        """
        if not outcome.text:
            handoff = render_handoff(
                outcome=outcome,
                task=task,
                failures=failures,
                artifacts=self.artifacts,
                trace_path=trace_path,
                continuations=int(getattr(session, "continuations", 0) or 0),
            )
            if handoff:
                outcome = replace(outcome, handoff=handoff)
        append_digest(
            session,
            build_digest(
                turn=turn_number,
                user_input=user_input,
                outcome=outcome,
                artifacts=self.artifacts,
                task=task,
                model_note=model_note,
                note_chars=int(
                    getattr(self._settings, "working_memory_note_chars", 120) or 120
                ),
            ),
            keep=int(getattr(self._settings, "working_memory_keep", 12)),
        )
        self.finalize(session, outcome, observer)
        # W3: the reporter fills usage inside finalize (after the save), so a
        # second save is the only way the row reaches the file.  Zero-spend
        # turns write no row — and no extra save — keeping save-count tests
        # and idle sessions exactly where they were.
        filled = self.last_outcome
        if filled is not None and filled.usage and any(filled.usage.values()):
            session.record_turn_usage(turn_number, filled.usage)
            self.save(session)
        return outcome

    # ---- introspection -----------------------------------------------------

    @property
    def last_artifacts(self) -> list[ToolArtifact]:
        """The current turn's artifacts (kept as a name for the loop's API)."""
        return self.artifacts
