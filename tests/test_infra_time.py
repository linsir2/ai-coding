from datetime import datetime, timedelta, timezone

from ai_coding.infra.time_utils import now_local, now_utc, parse_iso8601

# ---- now_local: C-4 gives the model a clock it can trust ------------------

# ISO-8601 with a *numeric* offset. No zone name: a TUI has no client channel
# to learn the user's IANA zone (dsh reads it from the browser) and an
# abbreviation lies -- CST is both China and Central Standard.
_ISO_WITH_OFFSET = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"


def test_now_local_carries_a_numeric_offset_rather_than_a_bare_wall_clock():
    import re

    assert re.fullmatch(_ISO_WITH_OFFSET, now_local())


def test_now_local_is_the_same_instant_as_utc_not_a_shifted_clock():
    """A wrong local zone would not be a formatting bug but a lie about when
    things happened, so the value must denote *this* moment, not the same
    digits in another zone."""
    parsed = parse_iso8601(now_local())
    assert parsed is not None
    assert parsed.tzinfo is not None
    drift = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert drift < 5, f"now_local is {drift:.1f}s away from real now"


# ---- now_utc stays what memory and the session store expect ----------------


def test_now_utc_still_ends_in_z():
    """The sibling helper has a dozen callers (memory notes, session stamps);
    touching this module must not move their format."""
    assert now_utc().endswith("Z")


# ---- parse_iso8601: the one parser for a stamp the project emits ------------
#
# ``datetime.fromisoformat`` only learned the ``Z`` designator in 3.11, and 3.10 is
# this project's declared floor (pyproject ``python_version``/``target-version``, and
# the CI matrix).  Every consumer used to normalize ``Z`` by hand, and the one test
# that parsed a stamp without normalizing failed on 3.10 only -- see
# docs/plan-ci-fix.md section 3.


def test_parse_iso8601_reads_a_z_suffix():
    """``Z`` is the UTC designator ``now_utc()`` emits; the floor must read it."""
    parsed = parse_iso8601("2026-10-25T08:56:33.371428Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_parse_iso8601_reads_a_numeric_offset():
    """``now_local()`` emits an offset; it must survive the round trip."""
    parsed = parse_iso8601("2026-10-25T16:56:33+08:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=8)


def test_parse_iso8601_keeps_a_bare_wall_clock_naive():
    """A pure parser: "what does a bare wall clock mean" is each caller's policy.

    The OTLP exporter hands a naive value to ``datetime.timestamp()`` (local), the
    memory relevance scorer reads one as UTC.  Attaching a zone here would silently
    move one of those two.
    """
    parsed = parse_iso8601("2026-10-25T08:56:33")
    assert parsed is not None
    assert parsed.tzinfo is None


def test_parse_iso8601_rejects_what_is_not_a_stamp():
    assert parse_iso8601("") is None
    assert parse_iso8601("not-a-time") is None
    assert parse_iso8601("2026-13-45T99:99:99Z") is None


def test_parse_time_still_reads_a_bare_wall_clock_as_utc():
    """The consumer contract, not the parser's: relevance treats naive as UTC.

    ``parse_time`` is exported from ``memory/relevance`` and consumed by
    ``memory/service.py`` and ``memory/store.py``; its naive->UTC policy must survive
    the move to the shared parser.
    """
    from ai_coding.memory.relevance import parse_time

    aware = parse_time("2026-10-25T08:56:33Z")
    naive = parse_time("2026-10-25T08:56:33")
    assert aware is not None and naive is not None
    assert naive.tzinfo is not None, "a bare wall clock must be read as UTC"
    assert naive == aware
    assert parse_time("") is None
    assert parse_time("not-a-time") is None
