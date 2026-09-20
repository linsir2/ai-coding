"""Time helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> str:
    """Current UTC time as an ISO-8601 string ending in ``Z``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
