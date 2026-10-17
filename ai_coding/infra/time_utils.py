"""Time helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> str:
    """Current UTC time as an ISO-8601 string ending in ``Z``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def now_local() -> str:
    """Local wall time with a numeric offset -- the only shape that cannot lie.

    ISO-8601 with the offset (``2026-10-17T16:30:12+08:00``) and no zone name:
    a TUI has no client channel to learn the user's IANA zone (dsh reads it
    from the browser), and an abbreviation lies -- ``CST`` is both China and
    Central Standard.  Seconds rather than minutes because the environment
    block is rebuilt every iteration, and minutes would freeze for the length
    of a turn.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")
