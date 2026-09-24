"""Compressing (and explicitly compacting) a conversation's history (R5).

The loop used to carry two near-identical wrappers around the same dependency:
``_compress`` for the budget-driven case and ``compact_session`` for the user's
explicit ``/compact``.  Both wrap the call in a deadline and degrade to the raw
history; both now live here, so the deadline discipline and the "never rewrite
the caller's history on failure" rule are stated once (docs/r5-analysis.md 3.3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from ai_coding.core.events import LoopEvent
from ai_coding.core.turn_report import emit_degraded
from ai_coding.domain.message import ChatMessage
from ai_coding.infra.deadline import Deadline, DeadlineExpired, remaining_of, run_with_deadline

__all__ = ["Compactor"]

_log = logging.getLogger(__name__)


class Compactor:
    """History compaction with a hard budget and a visible fallback."""

    def __init__(
        self,
        context: Any | None,
        *,
        emit: Callable[[LoopEvent], None],
    ) -> None:
        self._context = context
        self._emit = emit

    @staticmethod
    def did_compact(source: Sequence[ChatMessage], result: Sequence[ChatMessage]) -> bool:
        """True when ``compress`` actually rewrote the history.

        ``compress`` hands back the caller's own list when it does nothing (no
        context, below the water mark, or the degraded fallback) and a fresh list
        when it ran.  That identity is the loop's signal for "is the working-memory
        ring worth its tokens this request?" (docs/r6-analysis.md 2.1, D-R6-2).
        """
        return result is not source

    async def compress(
        self,
        history: list[ChatMessage],
        *,
        deadline: Deadline | None,
        on_summary: Callable[[str], None] | None = None,
        force: bool = False,
    ) -> list[ChatMessage]:
        """Compact only when the budget says so; degrade to the raw history.

        ``compress_if_needed`` is the R3b policy layer; an older injected context
        object may only implement ``compress``, so the newer method is preferred
        but not required.  ``force`` is what separates the budget-driven path from
        the user's explicit ``/compact`` — the latter must not be talked out of it
        by the watermark.
        """
        if self._context is None:
            return history
        budget = remaining_of(deadline)
        aware = getattr(self._context, "compress_if_needed", None)
        try:
            if aware is not None:
                return await run_with_deadline(
                    aware(history, on_summary=on_summary, force=force),
                    timeout=budget,
                    label="context_compress",
                )
            return await run_with_deadline(
                self._context.compress(history),
                timeout=budget,
                label="context_compress",
            )
        except Exception as exc:  # noqa: BLE001 — never break a turn
            _log.debug("context compression failed (%s); using the raw history", exc)
            self._degraded(
                "compression exceeded its budget"
                if isinstance(exc, DeadlineExpired)
                else "compression failed",
                "used the uncompressed history",
            )
            return history

    async def compact_now(
        self, session: Any, *, timeout: float
    ) -> dict[str, int]:
        """The user's explicit ``/compact``: force one page, replace the history.

        Unlike the budget-driven path this **does** rewrite the session — that is
        what was asked for — but only when the compression succeeded.  L3 persisted
        outputs, the session file and memory remain as safety nets either way.
        """
        before = len(session.messages)
        report = {"messages": before, "after": before, "removed": 0}
        if self._context is None or before == 0:
            return report
        budget = timeout if timeout > 0 else 60.0
        try:
            result = await self.compress(
                list(session.messages), deadline=Deadline.after(budget), force=True
            )
        except Exception:  # noqa: BLE001 — a REPL command must never crash
            return report
        if result == list(session.messages):
            return report  # nothing changed; do not pretend it did
        session.messages = list(result)
        after = len(session.messages)
        return {"messages": before, "after": after, "removed": before - after}

    # ---- internals ---------------------------------------------------------

    def _degraded(self, reason: str, fallback: str) -> None:
        """Report through the turn's own emit path (one shared construction)."""
        emit_degraded(self._emit, "context_compress", reason, fallback)
