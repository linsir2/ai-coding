"""Resume list model: pure grouping/filtering for the session picker.

No Textual here — ``ai_coding.ui.resume`` is plain data shaping (sorted input
assumed: ``SessionService.list_sessions`` is newest-first), so these run
offline and pin the exact labels the screen renders.
"""

from datetime import datetime, timedelta, timezone

from ai_coding.domain.session import SessionData
from ai_coding.ui.resume import filter_sessions, group_sessions_by_day


def _session(sid: str, last_access: str, preview: str = "", title: str = "") -> SessionData:
    return SessionData(
        session_id=sid,
        title=title,
        created_time=last_access,
        last_access_time=last_access,
        message_count=3,
        first_preview=preview,
    )


_NOW = datetime.now(timezone.utc)
# Noon local yesterday is yesterday in every timezone — a fixed "20:00Z"
# would already be tomorrow east of Greenwich and break the label.
_YESTERDAY_NOON = (_NOW.astimezone() - timedelta(days=1)).replace(
    hour=12, minute=0, second=0, microsecond=0
)


def test_groups_are_today_yesterday_then_absolute_dates():
    sessions = [
        _session("n" * 32, _NOW.isoformat(), "newest question"),
        _session("y" * 32, _YESTERDAY_NOON.isoformat(), "yesterday question"),
        _session("o" * 32, "2020-01-15T12:00:00Z", "old question"),
    ]
    groups = group_sessions_by_day(sessions, now=_NOW)

    assert [label for label, _ in groups] == ["Today", "Yesterday", "Wed Jan 15 2020"]
    assert [s.session_id for _, items in groups for s in items] == [
        "n" * 32, "y" * 32, "o" * 32,
    ]


def test_unparseable_timestamps_collect_under_unknown_without_raising():
    sessions = [_session("a" * 32, "garbage", "mystery")]
    groups = group_sessions_by_day(sessions, now=_NOW)

    assert groups == [("Unknown", sessions)]


def test_filter_matches_preview_title_and_id_prefix_case_insensitively():
    sessions = [
        _session("a" * 32, "2026-10-03T08:00:00Z", "Fix the PAYMENT refund bug"),
        _session("b" * 32, "2026-10-02T08:00:00Z", "Write docs", title="Onboarding"),
        _session("c" * 32, "2026-10-01T08:00:00Z", "Unrelated"),
    ]
    assert [s.session_id for s in filter_sessions(sessions, "payment")] == ["a" * 32]
    assert [s.session_id for s in filter_sessions(sessions, "onboard")] == ["b" * 32]
    assert [s.session_id for s in filter_sessions(sessions, "BBB")] == ["b" * 32]
    assert [s.session_id for s in filter_sessions(sessions, "")] == ["a" * 32, "b" * 32, "c" * 32]
    assert filter_sessions(sessions, "no-such-thing") == []
