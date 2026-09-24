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

Both are deliberately dumb: no filtering, no sampling, no mutation.
"""

from __future__ import annotations

import logging

from ai_coding.core.events import LoopEvent, LoopObserver, ScopedEvent

__all__ = ["Braid", "ScopedObserver"]

_log = logging.getLogger(__name__)


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
