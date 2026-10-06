"""The memory-facing half of a turn (R5).

Reading the constraint block, reconciling this turn's delta, waking consolidation
and reporting counts: all of it is *best-effort* by contract — memory must never
break a turn — and all of it now has one home instead of three methods spread
through the loop (``_memory_block``, the 72-line ``_harvest``, ``memory_status``).

Two rules are worth stating because they are easy to lose when the code moves:

* the harvest cursor lives **in the session**, not here, so a fresh loop (the CLI
  builds one per turn) does not re-harvest the whole transcript
  (docs/r3-analysis.md case 8);
* a failed harvest **must not advance that cursor** — losing messages is worse
  than skipping a turn of extraction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from ai_coding.core.events import LoopEvent, MemoryConsolidated, MemoryHarvested
from ai_coding.core.turn_report import emit_degraded
from ai_coding.domain.loop import TurnOutcome
from ai_coding.domain.session import SessionData
from ai_coding.infra.deadline import Deadline, remaining_of, run_with_deadline

__all__ = ["TurnMemory"]

_log = logging.getLogger(__name__)


class TurnMemory:
    """Everything a turn does about memory.  Never raises."""

    def __init__(
        self,
        memory: Any | None,
        *,
        emit: Callable[[LoopEvent], None],
        save: Callable[[SessionData], None],
    ) -> None:
        self._memory = memory
        self._emit = emit
        self._save = save

    # ---- reading -----------------------------------------------------------

    def block(self) -> tuple[str, tuple[str, ...]]:
        """The volatile constraint block for this request: ``(text, note ids)``.

        It goes in the request's *tail*, never the system prompt, so a change
        costs only that block's cache (docs/r3-analysis.md 1.13).
        """
        if self._memory is None:
            return "", ()
        try:
            notes = self._memory.index_notes()
            if not notes:
                return "", ()
            return self._memory.render_index_block(notes), tuple(
                note.id for note in notes
            )
        except Exception:  # noqa: BLE001 — memory must never break a turn
            _log.debug("memory index block failed", exc_info=True)
            self._degraded(
                "memory_block",
                "memory index could not be rendered",
                "no constraint block was injected",
            )
            return "", ()

    def status(self) -> dict[str, Any]:
        """Counts for ``/memory`` and ``/status``; never raises."""
        empty = {"total": 0, "active": 0, "pinned": 0, "by_kind": {}, "enabled": False}
        if self._memory is None:
            return empty
        try:
            return dict(self._memory.status())
        except Exception:  # noqa: BLE001 — memory is best-effort
            return {**empty, "enabled": True}

    # ---- writing -----------------------------------------------------------

    async def harvest(
        self,
        session: SessionData,
        *,
        deadline: Deadline | None,
        summaries: Sequence[str],
        outcome: TurnOutcome,
        turn: int | None = None,
        evidence: str = "",
    ) -> None:
        """Reconcile **this turn's delta** into memory, then consolidate.

        The delta is everything after ``session.harvested_messages``; the summary
        (when a compaction produced one) rides along as extra context.
        ``turn`` is the session-level turn number (R6d: it is what ``source_turn``
        should mean, instead of the message count), and ``evidence`` says whether
        this turn had a tool call succeed.  Both default to the R5 behaviour so
        existing callers and tests are unaffected.
        """
        if self._memory is None:
            return
        start = int(getattr(session, "harvested_messages", 0) or 0)
        delta = session.messages[start:]
        if not delta:
            return
        summary = "\n".join(text for text in summaries if text) or None
        try:
            report = await run_with_deadline(
                self._memory.harvest(
                    delta,
                    turn_index=len(session.messages) if turn is None else int(turn),
                    session_id=session.session_id,
                    summary=summary,
                    evidence=evidence,
                    # R11c: where this delta starts in the transcript, so a note
                    # written from it can point back at the messages it came from.
                    source_offset=start,
                ),
                timeout=remaining_of(deadline),
                label="memory_harvest",
            )
            session.harvested_messages = len(session.messages)
            self._save(session)
            if (
                report.added
                or report.updated
                or report.superseded
                or report.skipped
                or report.evicted
            ):
                self._emit(
                    MemoryHarvested(
                        added=tuple(report.added),
                        updated=tuple(report.updated),
                        superseded=tuple(report.superseded),
                        skipped=tuple(report.skipped),
                        evicted=tuple(report.evicted),
                        evidence=evidence,
                    )
                )
        except Exception:  # noqa: BLE001 — memory is best-effort, and now visible
            _log.debug("memory harvest failed", exc_info=True)
            self._degraded(
                "memory_harvest",
                "this turn's delta could not be reconciled",
                "memory was left unchanged",
            )
            return
        if outcome.text is None:
            return  # nothing was concluded; no reason to wake consolidation
        try:
            report = await run_with_deadline(
                self._memory.consolidate(),
                timeout=remaining_of(deadline),
                label="memory_consolidate",
            )
            # The same guard the harvest emit uses.  Consolidating on every turn
            # that concluded something means this reports rarely, which is the
            # point: it fires when a note actually changed or left.
            if report.merged or report.promoted or report.expired or report.evicted:
                self._emit(
                    MemoryConsolidated(
                        merged=tuple(report.merged),
                        promoted=tuple(report.promoted),
                        expired=tuple(report.expired),
                        evicted=tuple(report.evicted),
                    )
                )
        except Exception:  # noqa: BLE001 — memory is best-effort
            _log.debug("memory consolidation failed", exc_info=True)
            self._degraded(
                "memory_consolidate",
                "index consolidation failed",
                "the index was left as it was",
            )

    # ---- internals ---------------------------------------------------------

    def _degraded(self, component: str, reason: str, fallback: str) -> None:
        """Report through the turn's own emit path (one shared construction)."""
        emit_degraded(self._emit, component, reason, fallback)
