"""Resume list model: group + filter for the session picker (pure, no Textual).

``SessionService.list_sessions`` is newest-first, and these helpers preserve
that order inside every group — the screen only renders what they return.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from ai_coding.domain.session import SessionData


def _parse(ts: str) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def group_sessions_by_day(
    sessions: Sequence[SessionData], now: datetime | None = None
) -> list[tuple[str, list[SessionData]]]:
    """Bucket sessions under day labels, newest day first.

    Labels are ``Today`` / ``Yesterday`` / ``Mon Sep 28 2026``.  A session
    whose timestamp cannot be parsed still shows up — under ``Unknown`` —
    because dropping it would make a conversation unresumable with no trace.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.astimezone().date()
    buckets: dict[str, list[SessionData]] = {}
    order: list[str] = []
    for session in sessions:
        parsed = _parse(session.last_access_time)
        if parsed is None:
            label = "Unknown"
        else:
            day = parsed.astimezone().date()
            if day == today:
                label = "Today"
            elif day == today - timedelta(days=1):
                label = "Yesterday"
            else:
                label = day.strftime("%a %b %d %Y")
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(session)
    return [(label, buckets[label]) for label in order]


def filter_sessions(sessions: Sequence[SessionData], query: str) -> list[SessionData]:
    """Case-insensitive substring match over preview, title and session id."""
    needle = query.strip().lower()
    if not needle:
        return list(sessions)
    return [
        s
        for s in sessions
        if needle in (s.first_preview or "").lower()
        or needle in (s.title or "").lower()
        or needle in s.session_id.lower()
    ]


__all__ = ["filter_sessions", "group_sessions_by_day", "local_hhmm"]


def local_hhmm(ts: str) -> str:
    """``HH:MM`` in local time for a row stamp; ``""`` when unparseable."""
    parsed = _parse(ts)
    if parsed is None:
        return ""
    return parsed.astimezone().strftime("%H:%M")
