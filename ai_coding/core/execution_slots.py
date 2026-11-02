"""One execution-slot pool for tools, children and fan-out (P2-C1).

The pool is the single capacity unit: a child run, a fan-out child, or one
concurrent tool group holds one slot.  A model step holds none.

The ``slot_held`` context variable is what keeps a nested tool group from
asking for a *second* slot while its child already owns one.  The variable is
set only by the code that owns a child slot (``Scheduler.run``, the runner's
own sync/background/follow-up paths); a tool group never sets it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = [
    "ExecutionSlotPool",
    "Slot",
    "enter_child",
    "holding_slot",
    "slot_held",
]

#: True while the current task (and its descendants) already owns a child slot.
slot_held: ContextVar[bool] = ContextVar("slot_held", default=False)


def holding_slot() -> bool:
    """Whether the current task already owns an execution slot."""
    return bool(slot_held.get())


@contextmanager
def enter_child() -> Iterator[None]:
    """Run a block as a child that already owns its slot."""
    token = slot_held.set(True)
    try:
        yield
    finally:
        slot_held.reset(token)


class Slot:
    """One acquired slot; ``release`` is idempotent."""

    __slots__ = ("_pool", "reason", "_released", "_key")

    def __init__(
        self, pool: ExecutionSlotPool, reason: str = "", key: str | None = None
    ) -> None:
        self._pool = pool
        self.reason = reason
        self._released = False
        self._key = key

    @property
    def released(self) -> bool:
        return self._released

    @property
    def key(self) -> str | None:
        return self._key

    def bind(self, key: str) -> None:
        """Attach a diagnostic key after a reservation was handed over."""
        if self._released:
            return
        if self._key is not None:
            self._pool._keys.discard(self._key)
        self._key = key
        self._pool._keys.add(key)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._key is not None:
            self._pool._keys.discard(self._key)
            self._key = None
        self._pool._release()


class ExecutionSlotPool:
    """A bounded pool of ``Slot`` objects (``max_slots >= 1``)."""

    def __init__(self, max_slots: int) -> None:
        self._max_slots = max(1, int(max_slots))
        self._active = 0
        self._keys: set[str] = set()
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def max_slots(self) -> int:
        return self._max_slots

    @property
    def active(self) -> int:
        """How many slots are currently held (tests and diagnostics)."""
        return self._active

    @property
    def active_keys(self) -> set[str]:
        """Keys of the named slots currently held (a read-only copy)."""
        return set(self._keys)

    def held(self) -> int:
        return self._active

    async def acquire(self, reason: str = "", *, key: str | None = None) -> Slot:
        """Wait for one slot (a child run queues rather than failing)."""
        while True:
            if self._active < self._max_slots:
                self._active += 1
                if key is not None:
                    self._keys.add(key)
                return Slot(self, reason, key)
            future: asyncio.Future[None] = (
                asyncio.get_running_loop().create_future()
            )
            self._waiters.append(future)
            try:
                await future
            finally:
                try:
                    self._waiters.remove(future)
                except ValueError:
                    pass

    def try_reserve(self, reason: str = "", *, key: str | None = None) -> Slot | None:
        """Take a slot without waiting; ``None`` means the pool is full."""
        if self._active >= self._max_slots:
            return None
        self._active += 1
        if key is not None:
            self._keys.add(key)
        return Slot(self, reason, key)

    def _release(self) -> None:
        if self._active <= 0:  # pragma: no cover - defensive
            return
        self._active -= 1
        # Wake every waiter, not just the head: the head may have been
        # cancelled between its resume and this wake, which used to leave a
        # free slot with nobody owning it.  Each waiter re-checks the count.
        waiters = list(self._waiters)
        self._waiters.clear()
        for future in waiters:
            if not future.done():
                future.set_result(None)
