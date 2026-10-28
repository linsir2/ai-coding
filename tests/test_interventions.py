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


def test_strategy_does_not_fire_on_a_neutral_round():
    """D (audit fix): a round where every call was neutral (plan_keep and the
    like) leaves ``successful_calls == 0`` too, so the stall branch used to
    read a working revision loop as stuck.  Whatever the streak of neutral
    rounds, the ladder must stay parked."""
    d = iv.select(
        _snap(iteration=5, attempted_calls=4, successful_calls=0, neutral_streak=4),
        phase=iv.PRE, limits=LIMITS,
    )
    assert d is None


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


# ---- search ladder (B1/B2/B5) ----


def test_search_is_silent_below_the_threshold():
    assert iv.select(_snap(search_streak=3), phase=iv.PRE, limits=LIMITS) is None


def test_search_l1_fires_on_the_streak_threshold():
    d = iv.select(_snap(search_streak=4, search_calls=4), phase=iv.PRE, limits=LIMITS)
    assert (d.gate, d.level, d.kind) == ("search", 1, "nudge")
    assert iv.SEARCH_GATE_LEAD in d.text


def test_search_l1_aftermath_escalates_to_l2():
    """B1: the second rung exists, and after R20 it says something else.

    This test used to assert the rung named ``ruff check``/``mypy`` -- "that is
    its whole job".  R20/RC-1 measured why that was wrong: after the count rung
    was gated on *no new evidence*, L2 means "you re-read the same place three
    times", where a static check is off-topic, and the old text pushed T2 into
    eleven ``ruff``/``grep``/``find`` calls its own checkpoint had already ruled
    out.  The escalation itself is unchanged; the advice is not.
    """
    d = iv.select(
        _snap(
            search_streak=8,
            search_calls=8,
            intervention_ledger=_fired("search", 1, outcome="failed"),
        ),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert (d.gate, d.level, d.kind) == ("search", 2, "nudge")
    assert "停止重读" in d.text


def test_search_never_ends_the_turn():
    """The bare ladder gives no finish rung to a turn with *no positive evidence*.

    I1/R-7 changed the contract only in this sense: when the books also report
    stale/repeated evidence (or the loop opts into ``search_streak_l3``), L3
    *does* end the turn.  A bare ``LadderLimits()`` (l3 off, stale fields at
    their snapshots' defaults) keeps the old behaviour pinned here.
    """
    d = iv.select(
        _snap(
            search_streak=99,
            search_calls=99,
            intervention_ledger=_fired("search", 2, outcome="failed", at=(3, 4)),
        ),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert d.kind == "nudge"


def test_search_outcome_is_audit_only_not_a_decision_input():
    """``_helped`` answers False for SEARCH: the rung comes from the streak.

    The ledger still records the outcome for the audit trail, and ``_search_due``
    never reads it -- pinned here so nobody re-wires escalation to it by accident.
    """
    ledger = iv.settle(
        _fired("search", 1, at=(3, 4), ref={"streak": 4, "calls": 4}),
        _snap(iteration=5, search_streak=1, search_calls=5),
    )
    assert ledger["search"]["outcome"] == "failed"
    # Same decision whether the outcome says helped or failed.
    helped = _fired("search", 1, outcome="helped", ref={"streak": 4, "calls": 4})
    failed = _fired("search", 1, outcome="failed", ref={"streak": 4, "calls": 4})
    for ledger_in in (helped, failed):
        assert (
            iv.select(
                _snap(search_streak=4, search_calls=4, intervention_ledger=ledger_in),
                phase=iv.PRE,
                limits=LIMITS,
            )
            is None
        )


def test_search_threshold_zero_disables_the_gate():
    limits = iv.LadderLimits(search_streak_threshold=0)
    assert iv.select(_snap(search_streak=99), phase=iv.PRE, limits=limits) is None


def test_strategy_wins_over_search_when_both_trigger():
    snap = _snap(failed_streak=3, search_streak=9)
    d = iv.select(snap, phase=iv.PRE, limits=LIMITS)
    assert d.gate == "strategy"


def test_search_does_not_fire_at_the_post_step_seam():
    """The model stopped looking by itself; no rung to climb there (plan 6.3)."""
    assert iv.select(_snap(search_streak=99), phase=iv.POST, limits=LIMITS) is None


def test_search_goes_quiet_while_its_nudge_is_fresh():
    """The livelock regression net (found end to end, plan §十五).

    A nudge is injected *before* the model runs, so a gate that re-arms from
    window bookkeeping alone fires again immediately -- and the model never gets
    to act on the advice it was just given.
    """
    ledger = _fired("search", 1, outcome="pending", ref={"streak": 4, "calls": 4})
    assert (
        iv.select(
            _snap(search_streak=4, search_calls=4, intervention_ledger=ledger),
            phase=iv.PRE,
            limits=LIMITS,
        )
        is None
    )


def test_search_escalates_from_the_streak_not_from_the_window():
    """L2 is earned by the streak itself (6 looks), with no window bookkeeping."""
    d = iv.select(_snap(search_streak=6, search_calls=6), phase=iv.PRE, limits=LIMITS)
    assert (d.gate, d.level) == ("search", 2)


def test_search_stays_quiet_while_the_streak_crawls():
    """One extra look is not news; a whole threshold of them is."""
    quiet = _fired("search", 1, outcome="failed", ref={"streak": 4, "calls": 4})
    assert (
        iv.select(
            _snap(search_streak=5, search_calls=5, intervention_ledger=quiet),
            phase=iv.PRE,
            limits=LIMITS,
        )
        is None
    )


def test_search_speaks_again_after_a_whole_threshold_of_new_looks():
    again = _fired("search", 2, outcome="failed", ref={"streak": 6, "calls": 6})
    d = iv.select(
        _snap(search_streak=10, search_calls=10, intervention_ledger=again),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert (d.gate, d.level) == ("search", 2)


def test_search_is_off_in_plan_and_read_only_postures():
    """Those postures are *supposed* to be look-only (measured false positive)."""
    for run in ({"mode": "plan"}, {"readonly": True}):
        assert (
            iv.select(_snap(search_streak=99, **run), phase=iv.PRE, limits=LIMITS)
            is None
        )


def test_state_only_tools_cannot_hide_a_stall():
    """A todo write is not an action; the books are the ones that know (T14b)."""
    snap = _snap(search_streak=4, search_calls=4)
    assert iv.select(snap, phase=iv.PRE, limits=LIMITS).gate == "search"


# ---- I1/R-7: the L3 hard stop (off by default keeps B1/B2 unchanged) ----


def test_search_l3_is_off_by_default():
    """``LadderLimits()`` is the compatibility contract: 0 means the rung is off.

    The loop opts in through ``AIConfig.search_streak_l3``; a bare limits object
    (every pre-existing search unit test) must behave exactly as it did before
    the rung existed -- even at 99 looks with an exhausted ledger.
    """
    limits = iv.LadderLimits()
    assert limits.search_streak_l3 == 0
    d = iv.select(
        _snap(
            search_streak=99,
            search_calls=99,
            intervention_ledger=_fired("search", 2, outcome="failed"),
        ),
        phase=iv.PRE,
        limits=limits,
    )
    assert d is not None and d.kind == "nudge"


def test_the_product_default_for_the_look_count_is_off():
    """eval-08 measured the count rung selecting the wrong turns.

    A design question made 18 consecutive look-only calls and *every one* was a
    new finding (``stale_streak=0``, ``novel=18``); the count rung cut the turn
    and the answer shrank from 6,734 to a handoff report.  The survey it targets
    interleaves ``bash``, so its streak never even reached 7.  Turning this back
    on has to be a deliberate act, not a default drift.
    """
    from ai_coding.config.models import AIConfig
    from ai_coding.core.turn_policy import TurnPolicyConfig

    assert AIConfig().search_streak_l3 == 0
    assert TurnPolicyConfig.from_ai_config(AIConfig()).search_streak_l3 == 0


# ---- R20/RC-1: the count rung needs evidence, not just a count ------------


def test_the_count_rung_is_off_while_every_look_is_new():
    """RC-1, the regression this whole block exists for.

    Measured on T2's trace: ``{"iteration": 3, "level": 1, "calls": 4,
    "streak": 4, "novel": 4, "repeated": 0, "stale_streak": 0}`` -- every look
    was new evidence and the gate fired anyway, then told the model to stop
    doing the one thing that was working.  It answered each nudge with ``ruff``,
    which reset the streak, which re-armed the gate: eleven iterations spent on
    static checks instead of the answer.
    """
    snap = _snap(search_streak=4, search_calls=4, novel_findings=4)
    assert iv.select(snap, phase=iv.PRE, limits=LIMITS) is None


def test_a_novel_streak_of_six_stays_quiet_too():
    """The rung is ``search_streak >= 4``; six novel looks are still six novel
    looks, so the gate must not escalate to L2 either."""
    snap = _snap(search_streak=6, search_calls=6, novel_findings=6)
    assert iv.select(snap, phase=iv.PRE, limits=LIMITS) is None


def test_four_looks_without_any_new_evidence_still_nag():
    """The rung keeps its original job: a streak that produced nothing."""
    d = iv.select(
        _snap(search_streak=4, search_calls=4, novel_findings=0),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert (d.gate, d.level, d.kind) == ("search", 1, "nudge")


def test_the_hard_stop_still_closes_an_all_novel_look_only_turn():
    """The trap in the first draft of this fix.

    L3 walks ``_triggered`` too, so gating the count rung naively switched the
    hard stop off as well -- and ``test_a_turn_of_only_looks_ends_itself``
    (sixteen *novel* looks) stopped finishing.  L3 must be judged on the streak
    alone, before the evidence gate.
    """
    snap = _snap(search_streak=16, search_calls=16, novel_findings=16)
    d = iv.select(snap, phase=iv.PRE, limits=iv.LadderLimits(search_streak_l3=16))
    assert (d.gate, d.level, d.kind) == ("search", 3, "finish")


def test_the_stale_rungs_are_untouched():
    """The evidence fingerprint keeps earning rungs earlier than the streak."""
    snap = _snap(search_streak=5, search_calls=5, novel_findings=2, stale_streak=3)
    d = iv.select(snap, phase=iv.PRE, limits=LIMITS)
    assert (d.gate, d.level) == ("search", 1)


def test_the_nudge_says_the_streak_earned_no_new_evidence():
    """The text has to tell the truth about why it fired.  The count rung fires
    on a streak that produced nothing -- and this snapshot is that case."""
    d = iv.select(
        _snap(search_streak=4, search_calls=4, novel_findings=0),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert "没有带来任何新证据" in d.text


def test_a_stale_streak_does_not_claim_there_was_no_new_evidence():
    """The stale rung fires on a run of repeats *after* some new evidence, so
    "no new evidence" would be a lie the model can check against its own
    transcript.  It says what is actually true instead."""
    d = iv.select(
        _snap(search_streak=5, search_calls=5, novel_findings=2, stale_streak=3),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert "重复已经看过" in d.text
    assert "没有带来任何新证据" not in d.text


def test_the_second_rung_stops_pushing_static_checks():
    """Decision 6.

    After the fix L2 means "you re-read the same place three times", where
    ``ruff`` is off-topic.  Measured: T2's eleven ``bash`` calls were all
    ``ruff``/``grep``/``find`` while the model's own checkpoint had already
    written "ruff check . 未能暴露运行时逻辑缺陷" -- it knew the path was dead
    and the gate kept pushing it back onto it.
    """
    d = iv.select(
        _snap(
            search_streak=8,
            search_calls=8,
            intervention_ledger=_fired("search", 1, outcome="failed"),
        ),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert (d.gate, d.level, d.kind) == ("search", 2, "nudge")
    assert "ruff check" not in d.text
    assert "mypy" not in d.text
    assert "停止重读" in d.text


def test_the_second_rung_does_not_name_a_block_that_may_not_exist():
    """The ledger block only appears once something has been *read*; L2 can fire
    on grep/glob alone.  Pointing at it outright would send the model looking for
    a block that is not in the request."""
    d = iv.select(
        _snap(
            search_streak=8,
            search_calls=8,
            intervention_ledger=_fired("search", 1, outcome="failed"),
        ),
        phase=iv.PRE,
        limits=LIMITS,
    )
    assert "请求里有 `<read-ledger>` 块时" in d.text


def test_the_hard_stop_is_independent_of_the_count_switch():
    """``search_streak_threshold=0`` disables the count and stale rungs; L3 has
    its own switch and must still be able to close a look-only turn.

    Pinned because the first draft of the RC-1 fix read the threshold check as
    "off means the whole gate is off" and silently dropped the hard stop too.
    """
    limits = iv.LadderLimits(search_streak_threshold=0, search_streak_l3=4)
    d = iv.select(
        _snap(search_streak=4, search_calls=4, novel_findings=4),
        phase=iv.PRE,
        limits=limits,
    )
    assert (d.gate, d.level, d.kind) == ("search", 3, "finish")
