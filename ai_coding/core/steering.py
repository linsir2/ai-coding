"""Where a line typed while a turn is running waits (R7e-2).

Measured before it was designed (docs/r7e-analysis.md 1.1): a background reader can
hand a line over mid-turn, but only as an async task - a thread reader delivers the
line and then refuses to stop.  This queue is the receiving end of that reader, and
it is deliberately dumb: a bounded FIFO with no idea who filled it, so a
``turn.start`` hook or a scripted source can use it too.

Two rules matter and both have tests: draining is capped per call (the rest stays,
because silently dropping what the user typed is the one unforgivable bug here), and
a line starting with ``/`` is a command - it is refused here so the caller can keep
it for the REPL, which is what Claude Code documents for commands (docs §2.1b).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

__all__ = ["SteeringQueue"]

_TRUNCATION = "…"


class SteeringQueue:
    """A bounded FIFO of lines, thread-safe on put and drain.

    It used to hold lines the user typed mid-turn; those now live in the
    session's inbox table (D-2).  What remains is the notices lane -- harness
    lines (background completions) drained at iteration boundaries -- which
    teams the same shape for a different speaker, so the class stays and only
    the user-input half of its story left.
    """

    #: Called (from whatever thread queued the line) when a line was put with
    #: ``wake=True``.  The IPC layer installs it; nothing here calls the loop.
    on_put: Callable[[str], None] | None = None

    def __init__(self, *, max_messages: int = 5, max_chars: int = 2_000) -> None:
        self._lines: deque[str] = deque()
        self._max_messages = max(1, int(max_messages))
        self._max_chars = max(1, int(max_chars))

    def put(self, text: str, *, wake: bool = False) -> bool:
        """Queue one line; return ``False`` when it is a command, not an instruction.

        Safe from any thread: ``deque.append`` is atomic.

        ``wake`` (D-1c) marks a line that is worth starting a turn for when the
        session is idle -- a background job ending, not a sub-agent's report,
        which has always waited for the next human message.  The queue itself
        never wakes anything; it tells whoever registered :attr:`on_put`.
        """
        body = str(text or "").strip()
        if not body or body.startswith("/"):
            return False
        self._lines.append(body)
        if wake and self.on_put is not None:
            try:
                self.on_put(body)
            except Exception:  # noqa: BLE001 — a listener must not lose the line
                pass
        return True

    @property
    def pending(self) -> int:
        """How many lines are waiting (looking does not consume them)."""
        return len(self._lines)

    def drain(self) -> list[str]:
        """Take up to ``max_messages`` lines, oldest first, each cut to ``max_chars``."""
        taken: list[str] = []
        while self._lines and len(taken) < self._max_messages:
            body = self._lines.popleft()
            if len(body) > self._max_chars:
                body = body[: self._max_chars] + _TRUNCATION
            taken.append(body)
        return taken
