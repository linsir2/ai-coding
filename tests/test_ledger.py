"""Uncertainty ledger (R0-2, TDD): what the turn does not know, derived.

No LLM declarations: items are built mechanically from recorded
observations (failed verifies, tool errors, failure streaks), and an H level
only ever drops on recorded evidence.  The policy gates on the flat counter,
not on prose.
"""


from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig, TurnSnapshot
from ai_coding.domain.ledger import build_ledger, ledger_signature


def test_failed_verify_becomes_a_high_item_pointing_at_its_ac():
    items = build_ledger(
        failures=(), verify={"AC-2": (False, "assert 245 == 200")},
        failed_streak=0, iteration=3, succeeded_tools=frozenset(), stall_threshold=2,
    )
    assert len(items) == 1
    item = items[0]
    assert item.level == "high" and item.resolves == ("AC-2",)
    assert "245" in item.evidence[0]


def test_repeated_tool_error_escalates_and_later_success_closes_it():
    first = build_ledger(
        failures=(("bash", "nonzero_exit", "x"),), verify={},
        failed_streak=1, iteration=2, succeeded_tools=frozenset(), stall_threshold=2,
    )
    assert first[0].level == "medium"
    second = build_ledger(
        failures=(("bash", "nonzero_exit", "x"), ("bash", "nonzero_exit", "y")),
        verify={}, failed_streak=2, iteration=3,
        succeeded_tools=frozenset(), stall_threshold=2,
    )
    assert any(i.level == "high" for i in second)
    assert any(i.id.startswith("U-stall") for i in second)
    closed = build_ledger(
        failures=(("bash", "nonzero_exit", "x"),), verify={},
        failed_streak=0, iteration=4, succeeded_tools=frozenset({"bash"}),
        stall_threshold=2,
    )
    assert [i.id for i in closed if i.id.startswith("U-err")] == []


def test_signature_is_order_stable():
    left = build_ledger(
        failures=(("b", "e", "x"), ("a", "e", "y")), verify={},
        failed_streak=0, iteration=1, succeeded_tools=frozenset(), stall_threshold=2,
    )
    right = build_ledger(
        failures=(("a", "e", "y"), ("b", "e", "x")), verify={},
        failed_streak=0, iteration=1, succeeded_tools=frozenset(), stall_threshold=2,
    )
    assert ledger_signature(left) == ledger_signature(right)


def test_books_tracks_flat_rounds_and_resets_on_change():
    from ai_coding.domain.ledger import UncertaintyItem

    books = TurnBooks()
    item = UncertaintyItem(id="U-1", question="q?", kind="epistemic", level="high")
    assert books.note_ledger([]) is False
    assert books.note_ledger([item]) is True  # empty -> something: changed
    assert books.ledger_open == 1
    assert books.note_ledger([item]) is False  # same signature: no change
    assert books.note_ledger([]) is True  # emptied: changed
    assert books.ledger_open == 0


def test_policy_never_nudges_on_a_flat_ledger():
    """D1: the gate is retired — a flat ledger stays observable, not persuasive."""
    policy = TurnPolicy(TurnPolicyConfig())
    snapshot = TurnSnapshot(iteration=3, attempted_calls=1, successful_calls=1)
    decision = policy.decide_next_iteration(snapshot)
    assert decision.reason != "uncertainty ledger flat"
    assert decision.inject is None


def test_policy_acceptance_gate_nudges_then_finishes():
    from ai_coding.domain.acceptance import AcceptanceState

    policy = TurnPolicy(TurnPolicyConfig(max_acceptance_nudges=1))
    failing = (AcceptanceState(id="AC-1", passed=False),)
    first = policy.decide_after_step(TurnSnapshot(iteration=2, acceptance=failing))
    assert first.action.name == "NUDGE"
    assert "[acceptance-gate]" in (first.inject or "")
    spent = policy.decide_after_step(
        TurnSnapshot(iteration=3, acceptance=failing, acceptance_nudges=1)
    )
    assert spent.action.name == "FINISH"
    passing = (AcceptanceState(id="AC-1", passed=True),)
    done = policy.decide_after_step(TurnSnapshot(iteration=2, acceptance=passing))
    assert done.action.name == "FINISH" and "acceptance verified" in done.reason


def test_failure_question_shows_the_count_not_the_machine_code():
    """A5 T9: the ledger line is human-facing (bridge.py:148 -> the transcript
    only; nothing injects it into the model's context), so the raw failure code
    must not reach the screen.  The code stays in the item id, which is internal,
    and the evidence line below already carries the real failure output."""
    items = build_ledger(
        failures=(("bash", "nonzero_exit", "fatal: not a git repository"),) * 2,
        verify={}, failed_streak=2, iteration=2,
        succeeded_tools=frozenset(), stall_threshold=2,
    )
    question = next(i for i in items if i.id.startswith("U-err")).question
    assert "2 次" in question
    assert "nonzero_exit" not in question
