"""TDD (13 §2.1): the four intervention ladders as one pure state machine.

Table-driven on purpose: every behavioural rule of the ladder (trigger, window,
helped signal, escalation, three-state settlement, fire budget) is one row
here before it is one branch in ``ai_coding.core.interventions``.
"""

from ai_coding.core import interventions as iv
from ai_coding.core.turn_policy import TurnAction, TurnDecision, TurnSnapshot
from ai_coding.domain.acceptance import AcceptanceState
from ai_coding.domain.task import TodoItem


def _item(item_id: str, status: str = "pending") -> TodoItem:
    return TodoItem(id=item_id, content=f"do {item_id}", status=status)


def _snap(**over) -> TurnSnapshot:
    base = {"iteration": 3, "intervention_ledger": {}, "ask_user_calls": 0}
    base.update(over)
    return TurnSnapshot(**base)


LIMITS = iv.LadderLimits()


def _fired(gate, level, outcome="pending", fires=1, ref=None, at=(3, 5)) -> dict:
    entry = {"level": level, "at": list(at), "outcome": outcome, "fires": fires, "ref": ref or {}}
    return {gate: entry}


# ---- strategy ladder ----


def test_strategy_l1_fires_on_failed_streak():
    d = iv.select(_snap(failed_streak=2), phase=iv.PRE, limits=LIMITS)
    assert (d.gate, d.level, d.kind) == ("strategy", 1, "nudge")
    assert iv.STRATEGY_GATE_LEAD in d.text


def test_strategy_l1_fires_on_stall_without_streak_snapshot():
    d = iv.select(
        _snap(iteration=5, attempted_calls=5, successful_calls=0),
        phase=iv.PRE, limits=LIMITS,
    )
    assert (d.gate, d.level, d.kind) == ("strategy", 1, "nudge")


def test_strategy_observing_window_produces_nothing():
    snap = _snap(failed_streak=3, intervention_ledger=_fired("strategy", 1))
    assert iv.select(snap, phase=iv.PRE, limits=LIMITS) is None


def test_strategy_window_lapse_failed_escalates_to_l2_armed():
    ledger = iv.settle(
        _fired("strategy", 1, at=(3, 5)), _snap(iteration=5, failed_streak=3)
    )
    assert ledger["strategy"]["outcome"] == "failed"
    d = iv.select(_snap(iteration=5, failed_streak=3, intervention_ledger=ledger),
                  phase=iv.PRE, limits=LIMITS)
    assert (d.level, d.kind) == (2, "nudge")


def test_strategy_helped_resets_to_l1_and_disarms():
    ledger = iv.settle(_fired("strategy", 2), _snap(iteration=4, failed_streak=0))
    assert ledger["strategy"]["outcome"] == "helped"
    assert iv.armed(ledger) is False
    d = iv.select(_snap(failed_streak=2, intervention_ledger=ledger), phase=iv.PRE, limits=LIMITS)
    assert d.level == 1


def test_strategy_l3_lapse_finishes_no_progress():
    ledger = iv.settle(
        _fired("strategy", 3, at=(6, 8)), _snap(iteration=8, failed_streak=8)
    )
    d = iv.select(_snap(iteration=8, failed_streak=8, intervention_ledger=ledger),
                  phase=iv.PRE, limits=LIMITS)
    assert (d.level, d.kind) == (4, "finish")
    assert "no progress" in d.reason


def test_strategy_exhausted_restarts_at_l1_next_turn():
    d = iv.select(
        _snap(failed_streak=2, intervention_ledger=_fired("strategy", 4, outcome="exhausted")),
        phase=iv.PRE, limits=LIMITS,
    )
    assert d.level == 1


def test_unfinished_window_retries_same_level_next_turn():
    ledger = iv.settle(_fired("strategy", 2), _snap(iteration=9), turn_end=True)
    assert ledger["strategy"]["outcome"] == "unfinished"
    d = iv.select(_snap(iteration=1, failed_streak=2, intervention_ledger=ledger),
                  phase=iv.PRE, limits=LIMITS)
    assert d.level == 2


def test_strategy_beats_fatigue_in_one_iteration():
    snap = _snap(failed_streak=3, fatigue_index=99.0)
    d = iv.select(snap, phase=iv.PRE, limits=LIMITS)
    assert d.gate == "strategy"


# ---- fatigue ladder ----


def test_fatigue_l1_fires_above_threshold():
    d = iv.select(_snap(fatigue_index=35.0), phase=iv.PRE, limits=LIMITS)
    assert (d.gate, d.level) == ("fatigue", 1)


def test_fatigue_l1_helped_when_damage_stops_growing():
    ledger = iv.settle(
        _fired("fatigue", 1, ref={"fatigue": 35.0}),
        _snap(fatigue_index=35.0),
    )
    assert ledger["fatigue"]["outcome"] == "helped"


def test_fatigue_l3_lapse_finishes():
    ledger = _fired("fatigue", 3, outcome="failed", at=(2, 4))
    d = iv.select(_snap(iteration=4, fatigue_index=99.0, intervention_ledger=ledger),
                  phase=iv.PRE, limits=LIMITS)
    assert (d.level, d.kind, d.reason) == (3, "finish", "fatigue ladder exhausted")


# ---- task / acceptance ladders ----


def test_task_l1_refires_until_budget_then_asks():
    snap = _snap(open_items=(_item("1"),))
    assert iv.select(snap, phase=iv.POST, limits=LIMITS).level == 1
    snap = _snap(open_items=(_item("1"),), intervention_ledger=_fired("task", 1, fires=1))
    assert iv.select(snap, phase=iv.POST, limits=LIMITS).level == 1
    snap = _snap(open_items=(_item("1"),), intervention_ledger=_fired("task", 1, fires=2))
    d = iv.select(snap, phase=iv.POST, limits=LIMITS)
    assert d.level == 2
    assert iv.TASK_GATE_LEAD in d.text
    snap = _snap(open_items=(_item("1"),), intervention_ledger=_fired("task", 2, fires=1))
    d = iv.select(snap, phase=iv.POST, limits=LIMITS)
    assert (d.kind, d.reason) == ("finish", "todo ladder exhausted")


def test_task_l1_helped_when_items_move():
    ledger = iv.settle(
        _fired("task", 1, ref={"items": [["1", "pending"]]}),
        _snap(open_items=(_item("1", "in_progress"),)),
    )
    assert ledger["task"]["outcome"] == "helped"


def test_task_l2_helped_when_ask_user_appears():
    ledger = iv.settle(
        _fired("task", 2, ref={"asks": 0}), _snap(ask_user_calls=1)
    )
    assert ledger["task"]["outcome"] == "helped"


def test_acceptance_ladder_owns_the_post_slot_when_contracts_exist():
    failed = (AcceptanceState(id="a1", passed=False, output="boom"),)
    snap = _snap(acceptance=failed, open_items=(_item("1"),))
    d = iv.select(snap, phase=iv.POST, limits=LIMITS)
    assert d.gate == "acceptance"          # task 不与其竞争（现状行为保持）


# ---- bookkeeping ----


def test_record_writes_window_ref_and_fire_count():
    snap = _snap(iteration=4, failed_streak=2, ask_user_calls=0)
    decision = TurnDecision(TurnAction.NUDGE, "repeated tool failures",
                            intervention="strategy", level=1)
    ledger = iv.record({}, decision, snap)
    entry = ledger["strategy"]
    assert (
        entry["level"], entry["at"], entry["outcome"], entry["fires"]
    ) == (1, [4, 6], "pending", 1)
    ledger = iv.record(ledger, decision, snap)          # 同级再发 → fires 累加、窗口刷新
    assert ledger["strategy"]["fires"] == 2


def test_record_marks_finish_as_exhausted():
    decision = TurnDecision(TurnAction.FINISH, "todo ladder exhausted",
                            intervention="task", level=2)
    ledger = iv.record({}, decision, _snap())
    assert ledger["task"]["outcome"] == "exhausted"


def test_settle_drops_nothing_for_missing_ledger():
    assert iv.settle({}, _snap()) == {}
