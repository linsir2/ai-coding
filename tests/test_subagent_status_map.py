"""TDD (R4b): a new turn status must not turn into a fake error.

``TurnStatus.BUDGET`` is new, and the sub-agent packet maps turn statuses onto
its own vocabulary.  Without an entry it would fall through to ``"error"`` and
report "the sub-agent broke" when it actually just ran out of budget
(docs/r4-analysis.md 5.4).
"""

from ai_coding.core.subagent import _STATUS_MAP
from ai_coding.domain.loop import TurnStatus


def test_every_turn_status_has_a_mapping():
    for status in TurnStatus:
        assert status.value in _STATUS_MAP, f"{status.value} would fall back to 'error'"


def test_budget_reads_as_partial_work_not_as_an_error():
    assert _STATUS_MAP["budget"] == "max_iterations"


def test_the_existing_mappings_are_unchanged():
    assert _STATUS_MAP["completed"] == "completed"
    assert _STATUS_MAP["max_iterations"] == "max_iterations"
    assert _STATUS_MAP["timeout"] == "timeout"
    assert _STATUS_MAP["stopped"] == "error"
    assert _STATUS_MAP["aborted"] == "error"
    assert _STATUS_MAP["error"] == "error"
