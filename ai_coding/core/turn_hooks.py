"""The loop's half of the hook contract (R7a).

``core/hooks.py`` is the *framework* — the points, the runners and the failure
semantics a hook author picks from.  This is the other side: the two calls a turn
actually makes.

* :meth:`TurnHooks.context` asks ``turn.start`` whether it wants to add anything.
  The answer becomes a **volatile tail block**, never part of the system prompt and
  never persisted, which is what keeps R3's prefix-cache discipline intact
  (docs/r4-analysis.md 6.4).
* :meth:`TurnHooks.fire` fires an observation-only point, which by construction
  cannot change the turn.

Both are best-effort: a hook that raises is logged and the turn carries on, because
a user's plugin must never be able to stop the work (docs/r4-analysis.md 6.3).
"""

from __future__ import annotations

import logging
from typing import Any

from ai_coding.core.hooks import HookDecision, HookPoint
from ai_coding.domain.session import SessionData

__all__ = ["TurnHooks"]

_log = logging.getLogger(__name__)


class TurnHooks:
    """Run the two hook points a turn owns, degrading instead of raising."""

    def __init__(self, hooks: Any | None, *, workspace: str) -> None:
        self._hooks = hooks
        self._workspace = workspace

    async def context(self, session: SessionData) -> str:
        """What ``turn.start`` wants to inject, as one ``<hook-context>`` block."""
        if self._hooks is None:
            return ""
        try:
            results = await self._hooks.run(
                HookPoint.TURN_START,
                {
                    "session": session.session_id,
                    "workspace": self._workspace,
                    "messages": len(session.messages),
                },
            )
        except Exception:  # noqa: BLE001 — hooks must never break a turn
            _log.debug("turn.start hooks failed", exc_info=True)
            return ""
        parts = [
            result.context
            for result in results
            if result.decision is HookDecision.INJECT and result.context
        ]
        if not parts:
            return ""
        return "<hook-context>\n" + "\n".join(parts) + "\n</hook-context>"

    async def fire(self, point: HookPoint, session: SessionData) -> None:
        """Fire an observation-only hook point (never affects the turn)."""
        if self._hooks is None:
            return
        try:
            await self._hooks.run(
                point, {"session": session.session_id, "workspace": self._workspace}
            )
        except Exception:  # noqa: BLE001 — hooks must never break a turn
            _log.debug("hook point %s failed", point.value, exc_info=True)
