"""The four answers a posture request can have (M1b T10).

The old API returned a bool: "did it land now?".  That cannot say the two
things an operator actually needs to know — *is there something waiting?* and
*did you just cancel something?* — so a request that was already queued
looked exactly like a request that had just been applied.

The four states are dsh's (its ``set`` returns the same four), and the
differences from dsh are deliberate and small:

* dsh computes "where would we be" *including* a pending selection, so
  asking for the thing already queued is a ``noop``.  Ours answers
  ``QUEUED`` — the state is identical either way, but "it is queued" is the
  more honest thing to tell someone who just asked for it again.
* dsh's pending selection lives in a process-local ``WeakMap`` and is lost
  when the process dies.  Ours is written to a sidecar (M1b T10), so a
  queued switch survives a crash.

Enum members are truthy, so a caller that writes ``if not result`` will
silently take the wrong branch.  Compare with ``is``.
"""

from __future__ import annotations

import enum

__all__ = ["PostureResult"]


class PostureResult(enum.Enum):
    """What happened to a posture request."""

    #: It landed on the session now.
    COMMITTED = "committed"
    #: A turn is running; the request is waiting for the turn to close.
    QUEUED = "queued"
    #: The request withdrew a change that was already queued.
    CANCELLED = "cancelled"
    #: Nothing to do — the session is already in that state.
    NOOP = "noop"
