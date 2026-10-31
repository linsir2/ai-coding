"""Compressing (and explicitly compacting) a conversation's history (R5).

The loop used to carry two near-identical wrappers around the same dependency:
``_compress`` for the budget-driven case and ``compact_session`` for the user's
explicit ``/compact``.  Both wrap the call in a deadline and degrade to the raw
history; both now live here, so the deadline discipline and the "never rewrite
the caller's history on failure" rule are stated once (docs/r5-analysis.md 3.3).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ai_coding.core.events import (
    CompactionDelta,
    CompactionEnded,
    CompactionFailed,
    CompactionReason,
    CompactionSkipped,
    CompactionStarted,
    LayerStat,
    LoopEvent,
)
from ai_coding.core.turn_report import emit_degraded
from ai_coding.domain.message import ChatMessage
from ai_coding.infra.deadline import Deadline, DeadlineExpired, remaining_of, run_with_deadline

__all__ = ["Compactor"]

_log = logging.getLogger(__name__)


def _freed_tokens(layers: Sequence[LayerStat], before_messages: int, after_messages: int) -> bool:
    """Did this compaction actually invalidate the previous context reading?

    Layer stats are the measured answer when the delegate reports them, but they
    only cover the pipeline layers — ``hard_trim`` runs *after* them and is not
    a layer, so a compaction whose pages really dropped messages can still show
    ``after == before`` there.  Either signal counts: the estimate shrank, or the
    history did.  A delegate that reports no layers (the legacy
    ``compress``-only shape) has only the message count to go on.
    """
    if layers and layers[-1].after_tokens < layers[0].before_tokens:
        return True
    return after_messages < before_messages


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
        # One compaction at a time (see ``compress``): both entries — the
        # turn's assembly and the user's command — pass through it.
        self._lock = asyncio.Lock()
        #: The last ``compress`` applied an existing checkpoint view without
        #: compacting (see ``_compress_once``).  The assembler reads it so a
        #: view does not announce itself as a new compaction.  Reset per call.
        self.last_was_view = False

    @property
    def context_window(self) -> int | None:
        """The window the pipeline compacts against, or ``None`` without one.

        R22/H10: the referenced-file block must size itself from the same number
        the compactor uses -- a caller reading the raw config instead got the 48k
        default while a 6k session was running.  Duck-typed on purpose: the
        legacy ``compress``-only context objects and the test stubs simply have
        no window, and the block falls back to the default.
        """
        return getattr(self._context, "max_context_tokens", None)

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
        pinned: Sequence[ChatMessage] = (),
        session: Any | None = None,
        observed_tokens: int = 0,
        reason: CompactionReason = "auto",
    ) -> list[ChatMessage]:
        """Compact only when the budget says so; degrade to the raw history.

        ``compress_if_needed`` is the R3b policy layer; an older injected context
        object may only implement ``compress``, so the newer method is preferred
        but not required.  ``force`` is what separates the budget-driven path from
        the user's explicit ``/compact`` — the latter must not be talked out of it
        by the watermark.

        This is the one funnel every compaction passes through — the command, the
        budget-driven turn and the overflow fallback alike — so it is also where
        the context meter's boundary is written. It used to be written by the
        command alone, on a field name that does not exist, so it was never
        written at all and the meter kept reporting a context that had been
        thrown away.

        Lifecycle events (P3-2) are owned here too: Started before the delegate
        runs, Delta per summary chunk, Ended with the summary call's own usage,
        Failed when the summary call failed, Skipped when there was nothing to
        summarise.  Below-watermark turns emit nothing: the gate is probed
        before Started so an ordinary iteration stays silent.

        One compaction at a time per compactor: the lock serialises the two
        entries (turn assembly and the command) so a second caller waits and
        then usually finds nothing left to do — the safe half of OpenCode's
        inbox merge, without a queue.
        """
        if self._context is None:
            return history
        async with self._lock:
            return await self._compress_once(
                history,
                deadline=deadline,
                on_summary=on_summary,
                force=force,
                pinned=pinned,
                session=session,
                observed_tokens=observed_tokens,
                reason=reason,
            )

    async def _compress_once(
        self,
        history: list[ChatMessage],
        *,
        deadline: Deadline | None,
        on_summary: Callable[[str], None] | None = None,
        force: bool = False,
        pinned: Sequence[ChatMessage] = (),
        session: Any | None = None,
        observed_tokens: int = 0,
        reason: CompactionReason = "auto",
    ) -> list[ChatMessage]:
        """One funnel pass; the caller holds the lock (see ``compress``)."""
        self.last_was_view = False
        if not force and not self._should_run(history, observed_tokens):
            view = self._checkpoint_view(history, session=session, pinned=pinned)
            if view is not None:
                self.last_was_view = True
                return view
            return history
        self._emit(CompactionStarted(reason=reason))

        def _delta(text: str) -> None:
            self._emit(CompactionDelta(text=text))

        summaries: list[str] = []

        def _summary(text: str) -> None:
            summaries.append(text)
            if on_summary is not None:
                on_summary(text)

        result = await self._compress(
            history,
            deadline=deadline,
            on_summary=_summary,
            on_delta=_delta,
            force=force,
            pinned=pinned,
            observed_tokens=observed_tokens,
            reason=reason,
            session=session,
        )
        usage = self._summary_usage()
        if result is history:
            # The delegate gave up and handed the list straight back (the
            # exception path in _compress, which already reported Degraded +
            # Failed).  Nothing more to say.
            return history
        if result == history:
            # Content truth, not object identity: the pipeline always works
            # on a copy, so `did_compact` is True for even a no-op.  A job
            # that changed nothing is either a skip (nothing worth
            # summarising — muted) or a failure (the summary call itself
            # failed — red); the delegate's attempt bookkeeping says which.
            # The ORIGINAL list goes back so every downstream identity judge
            # (did_compact, ContextCompacted, the working-memory ring) agrees
            # nothing happened.
            if self._summary_attempted():
                self._emit(
                    CompactionFailed(
                        reason=reason,
                        error="summary unavailable (the model's reply was unusable, "
                        "or the summary call failed)",
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        total_tokens=usage.get("total_tokens", 0),
                    )
                )
            else:
                self._emit(CompactionSkipped(reason=reason))
            return history
        layers = self._layer_stats()
        # R22/A-8: raise the boundary only when the pipeline actually freed
        # tokens.  `/compact` on a short session reported 7 → 7 with every layer
        # at 681→681, yet the meter dropped to 0 tokens / 0% because the
        # boundary had been raised anyway -- `live_prompt_tokens` discards every
        # usage at or before it, so "nothing happened" read as "the window is
        # empty".  Content equality cannot decide this: the pipeline works on a
        # copy and can rewrite identical bodies into an unequal list.
        if session is not None and _freed_tokens(layers, len(history), len(result)):
            session.last_compacted_turn = int(getattr(session, "turn_count", 0) or 0)
        self._emit(
            CompactionEnded(
                reason=reason,
                before_messages=len(history),
                after_messages=len(result),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                text=summaries[-1] if summaries else "",
                layers=tuple(layers),
                estimated_before=layers[0].before_tokens if layers else 0,
                estimated_after=layers[-1].after_tokens if layers else 0,
            )
        )
        return result

    def _summary_attempted(self) -> bool:
        """Did the delegate actually try to summarise?  (Skip vs failure.)"""
        return bool(getattr(self._context, "last_summary_attempted", False))

    def view(
        self,
        history: list[ChatMessage],
        *,
        session: Any | None,
        pinned: Sequence[ChatMessage] = (),
    ) -> list[ChatMessage] | None:
        """The checkpoint view only, without the compaction gate.

        Used where the caller has already decided not to compact (an
        ``auto_compact: false`` session still has to send the checkpoint it
        already owns).
        """
        return self._checkpoint_view(history, session=session, pinned=pinned)

    def _checkpoint_view(
        self,
        history: list[ChatMessage],
        *,
        session: Any | None,
        pinned: Sequence[ChatMessage],
    ) -> list[ChatMessage] | None:
        """Ask the delegate for a checkpoint-shaped view, if it has one.

        Additive and probed: an older injected context object has no such
        method, and a view must never fail a turn -- ``None`` means "send the
        history as it is".
        """
        view = getattr(self._context, "checkpoint_view", None)
        if not callable(view):
            return None
        try:
            params: Mapping[str, Any] = inspect.signature(view).parameters
        except (TypeError, ValueError):
            params = {}
        try:
            if pinned and "pinned" in params:
                out = view(history, session=session, pinned=pinned)
            else:
                out = view(history, session=session)
        except Exception:  # noqa: BLE001 — a view is an optimisation, not a turn
            _log.debug("checkpoint view failed; sending the raw history", exc_info=True)
            return None
        if not out:
            return None
        return out if isinstance(out, list) else None

    def _should_run(self, history: list[ChatMessage], observed_tokens: int) -> bool:
        """True unless the delegate's own gate says this turn needs nothing.

        Unknown delegates (old fakes, legacy ``compress``-only objects) have no
        gate and always run; the real ``ContextManager.should_compact`` is
        probed by keyword so a below-watermark iteration emits no Started.
        """
        gate = getattr(self._context, "should_compact", None)
        if not callable(gate):
            return True
        try:
            params = inspect.signature(gate).parameters
        except (TypeError, ValueError):
            return True
        try:
            if "observed_tokens" in params:
                return bool(gate(history, observed_tokens))
            return bool(gate(history))
        except Exception:
            return True

    def _summary_usage(self) -> dict[str, int]:
        """The summary call's own usage for the Ended event (never the meter)."""
        raw = getattr(self._context, "last_summary_usage", None)
        if not isinstance(raw, dict):
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for key in out:
            value = raw.get(key, 0)
            if isinstance(value, int):
                out[key] = value
        return out

    def _layer_stats(self) -> list[LayerStat]:
        """The delegate's per-layer account of this run (``CompactionEnded.layers``).

        ``Compactor`` only ever read ``last_summary_usage``; without reading
        ``last_layer_stats`` here the new ``layers`` field would always be
        empty, which is the exact plumbing gap plan-m-e P0-1 closed.  Unknown
        delegates (the legacy ``compress``-only objects) simply report none.
        """
        raw = getattr(self._context, "last_layer_stats", None)
        if not isinstance(raw, (list, tuple)):
            return []
        return [stat for stat in raw if isinstance(stat, LayerStat)]

    async def _compress(
        self,
        history: list[ChatMessage],
        *,
        deadline: Deadline | None,
        on_summary: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        force: bool = False,
        pinned: Sequence[ChatMessage] = (),
        observed_tokens: int = 0,
        reason: CompactionReason = "auto",
        session: Any | None = None,
    ) -> list[ChatMessage]:
        if self._context is None:
            return history
        budget = remaining_of(deadline)
        aware = getattr(self._context, "compress_if_needed", None)
        try:
            if aware is not None:
                kwargs: dict[str, Any] = {
                    "on_summary": on_summary,
                    "force": force,
                }
                # An injected context may predate any of these gates, and a
                # compaction must never fail because of an argument it did not
                # ask for -- so every optional argument is probed, not just the
                # observed-token one.  ``pinned`` was added without the probe and
                # silently degraded every delegate that did not declare it.
                try:
                    params: Mapping[str, Any] = inspect.signature(aware).parameters
                except (TypeError, ValueError):
                    params = {}
                if pinned and "pinned" in params:
                    kwargs["pinned"] = pinned
                if observed_tokens and "observed_tokens" in params:
                    kwargs["observed_tokens"] = observed_tokens
                if on_delta is not None and "on_delta" in params:
                    kwargs["on_delta"] = on_delta
                if "reason" in params:
                    kwargs["reason"] = reason
                # I2: the checkpoint lives on the session, so the context
                # manager needs it to know what has already been summarised.
                if session is not None and "session" in params:
                    kwargs["session"] = session
                return await run_with_deadline(
                    aware(history, **kwargs),
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
            usage = self._summary_usage()
            self._emit(
                CompactionFailed(
                    reason=reason,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                )
            )
            return history

    async def compact_now(
        self, session: Any, *, timeout: float, reason: CompactionReason = "manual"
    ) -> dict[str, int]:
        """The user's explicit ``/compact``: force one page, replace the history.

        Unlike the budget-driven path this **does** rewrite the session — that is
        what was asked for — but only when the compression succeeded.  L3 persisted
        outputs, the session file and memory remain as safety nets either way.

        The snapshot is taken inside the lock: a second concurrent /compact
        waits, then plans against the history the first one left, so it finds
        nothing new instead of paying for a second summary.
        """
        if self._context is None:
            return {"messages": len(session.messages), "after": len(session.messages), "removed": 0}
        async with self._lock:
            before = len(session.messages)
            report = {"messages": before, "after": before, "removed": 0}
            if before == 0:
                return report
            budget = timeout if timeout > 0 else 60.0
            try:
                result = await self._compress_once(
                    list(session.messages),
                    deadline=Deadline.after(budget),
                    force=True,
                    session=session,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001 — a REPL command must never crash
                return report
            if result == list(session.messages):
                return report  # nothing changed; do not pretend it did
            # D-6: this is the only writer of `session.messages` in the codebase,
            # and it is the lossy one -- the summary replaces the file. The snip
            # stage archives the middle it drops, but only when the history is
            # long enough to trip max_messages, so on a short session nothing was
            # written and the original left the disk with this line. Archive it
            # while it still exists. Probed like the other delegate capabilities
            # above: a pre-archiving context object simply has no such method.
            archive = getattr(self._context, "archive_transcript", None)
            if callable(archive):
                archive(list(session.messages))
            session.messages = list(result)
            after = len(session.messages)
            return {"messages": before, "after": after, "removed": before - after}

    # ---- internals ---------------------------------------------------------

    def _degraded(self, reason: str, fallback: str) -> None:
        """Report through the turn's own emit path (one shared construction)."""
        emit_degraded(self._emit, "context_compress", reason, fallback)
