from datetime import datetime, timezone

from ai_coding.infra.time_utils import now_local, now_utc

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
    parsed = datetime.fromisoformat(now_local())
    assert parsed.tzinfo is not None
    drift = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert drift < 5, f"now_local is {drift:.1f}s away from real now"


# ---- now_utc stays what memory and the session store expect ----------------


def test_now_utc_still_ends_in_z():
    """The sibling helper has a dozen callers (memory notes, session stamps);
    touching this module must not move their format."""
    assert now_utc().endswith("Z")
