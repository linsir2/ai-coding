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


def parse_iso8601(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, ``None`` when it is not one.

    ``datetime.fromisoformat`` only learned the ``Z`` designator in 3.11, and 3.10 is
    this project's declared floor (``pyproject`` ``python_version``/``target-version``
    and the CI matrix) -- so every consumer normalized it by hand, and the one test
    that parsed a stamp without normalizing failed on 3.10 only.

    A pure parser: a value with no offset comes back *naive*, because "what does a bare
    wall clock mean" is a policy each caller owns -- the OTLP exporter hands it to
    ``datetime.timestamp()`` (local), the memory relevance scorer reads it as UTC.
    Putting either choice here would silently move the other's numbers.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
