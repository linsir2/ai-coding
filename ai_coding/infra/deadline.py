"""Hard deadlines for anything that waits on the outside world (R2a).

Two defects motivated this module (docs/r2-analysis.md section 1.3/1.4):

1. ``asyncio.wait_for`` awaits the *cancellation* before it raises, so a coroutine
   that is slow to acknowledge a cancel (``proc.communicate()`` while another
   process holds the pipe) makes the timeout unreachable.  ``run_with_deadline``
   cancels and **abandons**: it reports the timeout immediately and drains the
   leftover task in the background.
2. An event loop can be woken by another thread or by a signal only through its
   self-pipe.  When that write fails (e.g. a seccomp filter returning EPERM) the
   loop silently sleeps until its next timer, so completions signalled from a
   worker thread or by SIGCHLD are only noticed at the deadline itself.  The
   heartbeat tick keeps a timer in flight, which makes such a loop notice them
   within ``tick`` seconds.  It costs one wakeup per interval and is harmless on
   a healthy loop.

Everything that waits on subprocesses, threads or HTTP goes through here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = [
    "HEARTBEAT_SECONDS",
    "Deadline",
    "DeadlineExpired",
    "RunCancelled",
    "remaining_of",
    "run_with_deadline",
]

HEARTBEAT_SECONDS = 0.05

_T = TypeVar("_T")

_log = logging.getLogger(__name__)


class DeadlineExpired(Exception):
    """A hard budget ran out.  The awaitable was abandoned, not awaited."""

    def __init__(self, label: str, timeout: float) -> None:
        super().__init__(f"{label} exceeded its {timeout:g}s deadline")
        self.label = label
        self.timeout = timeout


class RunCancelled(Exception):
    """The caller asked to stop waiting (R8c).  The awaitable was abandoned too.

    Distinct from :class:`DeadlineExpired` on purpose: a deadline is the clock
    running out, this is a human deciding the call is not worth waiting for.  The
    dispatcher turns it into ``ToolResult.error = "cancelled_by_user"``.
    """

    def __init__(self, label: str) -> None:
        super().__init__(f"{label} was cancelled")
        self.label = label


@dataclass(frozen=True)
class Deadline:
    """An absolute point in time (monotonic clock, so it survives clock jumps)."""

    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        """A deadline ``seconds`` from now (negative or zero = already expired)."""
        return cls(expires_at=time.monotonic() + seconds)

    def remaining(self) -> float:
        """Seconds left; negative once the deadline has passed."""
        return self.expires_at - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0


def remaining_of(deadline: Deadline | None) -> float | None:
    """Seconds left, floored at zero; ``None`` when there is no deadline.

    Callers that pass this straight to ``run_with_deadline`` need "no ceiling" and
    "no time left" to be different answers, and neither may be negative — the same
    rule used to be written twice (docs/r5-analysis.md 1.3).
    """
    if deadline is None:
        return None
    return max(deadline.remaining(), 0.0)


def _drain(task: asyncio.Task[Any]) -> None:
    """Retrieve an abandoned task's outcome so asyncio never warns about it."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — draining is best-effort
        return
    if exc is not None:
        _log.debug("abandoned task finished with %r", exc)


def _abandon(
    task: asyncio.Task[Any],
    on_abandon: Callable[[asyncio.Task[Any]], None] | None,
) -> None:
    """Hand a task over to the background: cancel it, drain it, tell the caller."""
    task.cancel()
    task.add_done_callback(_drain)
    if on_abandon is not None:
        try:
            on_abandon(task)
        except Exception:  # noqa: BLE001 — an observer must not break the deadline
            _log.debug("on_abandon callback failed", exc_info=True)


async def _heartbeat(interval: float) -> None:
    """A timer that keeps firing; see the module docstring, point 2."""
    while True:
        await asyncio.sleep(interval)


async def run_with_deadline(
    awaitable: Awaitable[_T],
    *,
    timeout: float | None,
    label: str,
    tick: float = HEARTBEAT_SECONDS,
    on_abandon: Callable[[asyncio.Task[Any]], None] | None = None,
    cancel: asyncio.Event | None = None,
) -> _T:
    """Await ``awaitable`` with a hard ``timeout`` (``None`` = unbounded).

    On expiry the awaitable is cancelled and abandoned, and :class:`DeadlineExpired`
    is raised immediately — the caller is never blocked by a task that ignores
    cancellation.  ``on_abandon`` receives the leftover task (tests and tracing).

    R8c adds a second reason to abandon: ``cancel`` (an ``asyncio.Event`` the user's
    Esc key sets) stops the wait and raises :class:`RunCancelled`.  Both reasons
    share this one implementation, so "how to give up on an await" stays written
    once (the R2a discipline).
    """
    task: asyncio.Task[_T] = asyncio.ensure_future(awaitable)

    if timeout is not None and timeout <= 0:
        _abandon(task, on_abandon)
        raise DeadlineExpired(label, timeout)

    ticker: asyncio.Task[None] | None = None
    if tick > 0:
        ticker = asyncio.ensure_future(_heartbeat(tick))
        ticker.set_name("ai-coding-heartbeat")

    watcher: asyncio.Task[bool] | None = None
    if cancel is not None:
        watcher = asyncio.ensure_future(cancel.wait())
        watcher.set_name("ai-coding-cancel-watch")

    try:
        if timeout is None and watcher is None:
            return await task
        waiter: set[asyncio.Task[Any]] = {task} if watcher is None else {task, watcher}
        done, _pending = await asyncio.wait(
            waiter, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if watcher is not None and watcher in done:
            _abandon(task, on_abandon)
            raise RunCancelled(label)
        if task not in done:
            _abandon(task, on_abandon)
            raise DeadlineExpired(label, timeout or 0.0)
        return task.result()
    except asyncio.CancelledError:
        # The caller's task was cancelled while we waited on ``asyncio.wait``.
        # ``await task`` (the no-timeout path above) forwards cancellation to
        # the awaited task for free — it is the task's ``_fut_waiter`` — but
        # ``asyncio.wait`` waits on its own internal future, so the inner task
        # would be orphaned and keep running.  (Measured live: Esc left a bash
        # ``sleep 60`` alive because the dispatcher's wrapper dropped the tool.)
        # The timeout / cancel-event paths already abandoned it above and do
        # not raise CancelledError, so this only fires for external cancel.
        if not task.done():
            _abandon(task, on_abandon)
        raise
    finally:
        for helper in (ticker, watcher):
            if helper is not None and not helper.done():
                helper.cancel()
                helper.add_done_callback(_drain)
