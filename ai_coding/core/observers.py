"""Observer composition (R4a).

The loop and the dispatcher each talk to exactly one :class:`LoopObserver`.
These two adapters keep that seam small while letting R4 attach new listeners
without touching a single call site:

* :class:`Braid` fans one event out to many observers.  Each is isolated — a
  broken renderer must not stop the trace recorder, and neither may stop the
  turn (docs/r4-analysis.md P1/I7).
* :class:`ScopedObserver` is handed to a *nested* run (a sub-agent) so its events
  arrive in the parent's stream wrapped in :class:`ScopedEvent`, carrying the
  parentage needed to rebuild the execution tree (§4.6).
* :class:`SafeObserver` wraps the *one* observer a turn talks to and swallows its
  failures, counting them so the turn can still say "a renderer broke"
  (docs/r4-analysis.md 1.1).

All three are deliberately dumb: no filtering, no sampling, no mutation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ai_coding.core.events import LoopEvent, LoopObserver, ScopedEvent

__all__ = ["Braid", "EmitObserver", "SafeObserver", "ScopedObserver"]

_log = logging.getLogger(__name__)


class EmitObserver:
    """A :class:`LoopObserver` over a plain emit callable (R0-3).

    Tools receive ``ctx.emit``, not an observer object — this adapter lets a
    tool fan a nested run's events back into the parent stream without the
    tool layer importing observer machinery.  Dumb like the rest: no
    filtering, no mutation.  Failures propagate to the caller (the runner
    wraps the child loop in ``SafeObserver`` anyway); an emit that must never
    raise should arrive pre-wrapped.
    """

    def __init__(self, emit: Callable[[Any], None]) -> None:
        self._emit = emit

    def on_event(self, event: LoopEvent) -> None:
        self._emit(event)


class SafeObserver:
    """Wraps an observer so a UI failure can never break a turn.

    Rendering is a best-effort side channel: if it explodes we log and carry on,
    and the failure count is what makes the turn report a ``Degraded`` for it
    (docs/r1a-4-analysis.md §8 核验⑤).  R7a moved this here from the loop — it is
    an observer adapter, not part of the iteration.
    """

    def __init__(self, inner: LoopObserver) -> None:
        self._inner = inner
        # R4a: a renderer that keeps failing is still worth reporting once per
        # turn, instead of vanishing into a debug log (docs/r4-analysis.md 1.1).
        self.failures = 0

    def on_event(self, event: LoopEvent) -> None:
        try:
            self._inner.on_event(event)
        except Exception:  # noqa: BLE001 — UI failures must not kill the turn
            self.failures += 1
            _log.debug("loop observer failed on %s", type(event).__name__, exc_info=True)


class Braid:
    """Forward every event to each observer, isolating their failures."""

    def __init__(self, *observers: LoopObserver) -> None:
        self._observers: list[LoopObserver] = list(observers)

    def on_event(self, event: LoopEvent) -> None:
        for observer in self._observers:
            try:
                observer.on_event(event)
            except Exception:  # noqa: BLE001 — observation must never break the turn
                _log.debug(
                    "observer %s failed on %s",
                    type(observer).__name__,
                    type(event).__name__,
                    exc_info=True,
                )

    def __len__(self) -> int:
        return len(self._observers)


class ScopedObserver:
    """Wrap a nested run's events with their scope and parent span."""

    def __init__(self, scope: str, parent: str, inner: LoopObserver) -> None:
        self._scope = scope
        self._parent = parent
        self._inner = inner

    def on_event(self, event: LoopEvent) -> None:
        self._inner.on_event(
            ScopedEvent(scope=self._scope, parent=self._parent, event=event)
        )

    @property
    def scope(self) -> str:
        return self._scope
