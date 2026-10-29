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


def test_one_failure_is_not_called_a_repeat():
    """R21 §3: "反复失败（1 次）" contradicted itself on the first sighting.

    Only the sentence changes: the level rule (one failure is already worth a
    medium question) must stay, because it is what raises the item at all.
    """
    first = build_ledger(
        failures=(("bash", "nonzero_exit", "x"),), verify={},
        failed_streak=0, iteration=2, succeeded_tools=frozenset(), stall_threshold=2,
    )
    assert first[0].question == "bash 调用失败（1 次）：直接原因是什么？"
    assert first[0].level == "medium"

    twice = build_ledger(
        failures=(("bash", "nonzero_exit", "x"), ("bash", "nonzero_exit", "y")),
        verify={}, failed_streak=0, iteration=3,
        succeeded_tools=frozenset(), stall_threshold=2,
    )
    assert twice[0].question == "bash 反复失败（2 次）：直接原因是什么？"
    assert twice[0].level == "high"


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


def test_policy_acceptance_gate_nudges_then_asks_then_finishes():
    from ai_coding.domain.acceptance import AcceptanceState

    policy = TurnPolicy(TurnPolicyConfig(max_acceptance_nudges=1))
    failing = (AcceptanceState(id="AC-1", passed=False),)
    first = policy.decide_after_step(TurnSnapshot(iteration=2, acceptance=failing))
    assert first.action.name == "NUDGE"
    assert "[acceptance-gate]" in (first.inject or "")
    # 13 §2.1: a spent L1 budget escalates to the ask level, not to a stop.
    spent = policy.decide_after_step(
        TurnSnapshot(
            iteration=3, acceptance=failing, acceptance_nudges=1,
            intervention_ledger={"acceptance": {"level": 1, "at": [2, 4],
                                 "outcome": "pending", "fires": 1, "ref": {}}},
        )
    )
    assert spent.action.name == "NUDGE"
    assert (spent.intervention, spent.level) == ("acceptance", 2)
    # An ignored ask is where the ladder ends.
    ignored = policy.decide_after_step(
        TurnSnapshot(
            iteration=5, acceptance=failing,
            intervention_ledger={"acceptance": {"level": 2, "at": [3, 5],
                                 "outcome": "pending", "fires": 1, "ref": {}}},
        )
    )
    assert ignored.action.name == "FINISH"
    assert ignored.reason == "acceptance ladder exhausted"
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
