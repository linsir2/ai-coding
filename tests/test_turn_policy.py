"""TDD (R2c): TurnPolicy — task injection and the stop decision as a pure unit.

Extracted from ``core/agent_loop.py`` because "should this turn go on?" was the
part of the loop that needed to be read (and re-tested) as a whole.  Behaviour is
unchanged: the existing loop tests must keep passing untouched.
"""

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core import interventions
from ai_coding.core.interventions import TASK_GATE_LEAD
from ai_coding.core.turn_policy import (
    TASK_MESSAGE_LEAD,
    TaskInjection,
    TurnAction,
    TurnDecision,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)
from ai_coding.domain.task import TaskState, TodoItem, TodoStatus


def _item(item_id: str, status: TodoStatus = "pending") -> TodoItem:
    return TodoItem(id=item_id, content=f"do {item_id}", status=status)


def _config(**over) -> TurnPolicyConfig:
    base = {"stop_policy": "todo_aware", "max_todo_nudges": 2, "max_iterations": 10}
    base.update(over)
    return TurnPolicyConfig(**base)  # type: ignore[arg-type]


# ---- next iteration --------------------------------------------------------


def test_next_iteration_iterates_within_budget():
    decision = TurnPolicy(_config(max_iterations=3)).decide_next_iteration(
        TurnSnapshot(iteration=1)
    )
    assert decision.action is TurnAction.ITERATE


def test_next_iteration_finishes_when_the_iteration_budget_is_gone():
    decision = TurnPolicy(_config(max_iterations=3)).decide_next_iteration(
        TurnSnapshot(iteration=4)
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "iteration budget exhausted"


def test_next_iteration_finishes_when_the_turn_budget_is_gone():
    decision = TurnPolicy(_config()).decide_next_iteration(
        TurnSnapshot(iteration=1), budget_expired=True
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "turn deadline exceeded"


# ---- after a model step ----------------------------------------------------


@pytest.mark.parametrize(
    ("stop_policy", "open_items", "nudges", "expected"),
    [
        # natural ends as soon as the model stops, whatever the list says
        ("natural", (_item("1"),), 0, TurnAction.FINISH),
        ("natural", (), 0, TurnAction.FINISH),
        # nothing open -> the turn is over
        ("todo_aware", (), 0, TurnAction.FINISH),
        ("strict", (), 0, TurnAction.FINISH),
        # open items and budget left -> nudge
        ("todo_aware", (_item("1"),), 0, TurnAction.NUDGE),
        ("todo_aware", (_item("1"),), 1, TurnAction.NUDGE),
        ("strict", (_item("1"),), 99, TurnAction.NUDGE),  # strict ignores the cap
        # 13 (sanctioned ②): a spent budget now escalates to the ask level (L2);
        # where the ladder stops is test_interventions.py's business.
        ("todo_aware", (_item("1"),), 2, TurnAction.NUDGE),
    ],
)
def test_after_step_decision_table(stop_policy, open_items, nudges, expected):
    policy = TurnPolicy(_config(stop_policy=stop_policy, max_todo_nudges=2))
    decision = policy.decide_after_step(
        TurnSnapshot(iteration=1, nudges=nudges, open_items=open_items, final_text="done")
    )
    assert decision.action is expected


def test_nudge_decision_carries_the_message():
    policy = TurnPolicy(_config())
    decision = policy.decide_after_step(
        TurnSnapshot(iteration=1, open_items=(_item("1"),))
    )
    assert decision.inject is not None
    assert decision.inject.startswith(TASK_GATE_LEAD)
    assert "1. do 1" in decision.inject


def test_finish_reason_for_a_completed_step_is_stable():
    decision = TurnPolicy(_config()).decide_after_step(TurnSnapshot(iteration=1))
    assert decision.reason == "model finished"


# ---- 18 §10.5: the two posture axes silence the execution gates (POST only) --


@pytest.mark.parametrize("posture", [{"mode": "plan"}, {"readonly": True}])
def test_after_step_finishes_quietly_under_a_posture_once_the_plan_is_in(posture):
    """18 §10.5 的"执行门静默"保持：计划已提交时 posture 下照旧安静收尾。

    未提交的路径由 plan 提交守卫接管（下方三个用例）——守卫不是执行门：
    它不进干预账本（intervention 为空），只为"裸文本收尾、零提交"补一次
    补救与一行终局通知（7ad86804 / 1fd3be1e 两次实测）。"""
    decision = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(
            iteration=1, open_items=(_item("1"),), plan_submitted=True, **posture
        )
    )
    assert (
        decision.action,
        decision.reason,
        decision.inject,
        decision.intervention,
    ) == (TurnAction.FINISH, "model finished", None, "")


# ---- plan 提交守卫（stage 4 r2）: 裸文本收尾 → 一次补救 → 终局通知 ----------


def test_after_step_asks_for_the_plan_it_never_got():
    # R08: the guard speaks to the plan posture only; /readonly alone is not
    # planning (its own case lives below).
    decision = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(iteration=1, open_items=(_item("1"),), mode="plan")
    )
    assert (decision.action, decision.reason) == (TurnAction.NUDGE, "plan unsubmitted")
    assert decision.inject is not None and "present_plan" in decision.inject
    assert decision.intervention == ""  # 不是 ladder 家族，不进干预账本


def test_the_plan_gate_asks_even_with_an_empty_list():
    """P2 verification: planning no longer writes todo items, so the guard
    that asks for a submission must not depend on them.  It keys on
    ``plan_submitted`` (the tool_success record), never on ``open_items`` —
    this pins that, because the old workflow made the two look equivalent."""
    decision = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(iteration=1, mode="plan")  # no items, nothing submitted
    )
    assert (decision.action, decision.reason) == (TurnAction.NUDGE, "plan unsubmitted")

    submitted = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(iteration=1, mode="plan", plan_submitted=True)
    )
    assert (submitted.action, submitted.reason) == (TurnAction.FINISH, "model finished")


def test_the_plan_nudge_sends_the_steps_through_present_plan():
    """P1: the remedy text used to say "write the items with todo_write", which
    is the workflow the redesign removed (trace ec89d5ee: 8 steps, 8
    iterations, plus an add/clear loop).  The nudge now points at the items
    array on the same call."""
    decision = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(iteration=1, mode="plan")
    )
    assert decision.inject is not None
    assert "present_plan" in decision.inject
    assert "items" in decision.inject
    assert "todo_write" not in decision.inject


def test_after_step_stops_asking_after_one_remedy():
    decision = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(iteration=1, open_items=(_item("1"),), plan_nudged=True, mode="plan")
    )
    assert (decision.action, decision.reason) == (TurnAction.FINISH, "plan unsubmitted")


def test_readonly_alone_never_asks_for_a_plan():
    """R08: the submission guard speaks to the plan posture only — /readonly
    alone is not planning, so it must not be nagged to submit, and the
    execution gates stay silent exactly as before."""
    decision = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(iteration=1, open_items=(_item("1"),), readonly=True)
    )
    assert (decision.action, decision.reason) == (TurnAction.FINISH, "model finished")


def test_a_plain_turn_never_asks_for_a_plan():
    decision = TurnPolicy(_config()).decide_after_step(TurnSnapshot(iteration=1))
    assert (decision.action, decision.reason) == (TurnAction.FINISH, "model finished")


# ---- the stop seam (dsh turn-stopping): guards in order, first verdict wins ----


class _ScriptedGuard:
    """A stop guard scripted for the seam test; records that it was asked."""

    def __init__(self, name: str, verdict: TurnDecision | None, asked: list[str]):
        self._name = name
        self._verdict = verdict
        self._asked = asked

    def on_stop(self, snapshot: TurnSnapshot) -> TurnDecision | None:
        self._asked.append(self._name)
        return self._verdict


def test_stop_guards_run_in_order_and_short_circuit():
    asked: list[str] = []
    passing = _ScriptedGuard("first", None, asked)
    speaking = _ScriptedGuard(
        "second", TurnDecision(TurnAction.NUDGE, "second spoke", inject="补交"), asked
    )
    never = _ScriptedGuard("third", TurnDecision(TurnAction.FINISH, "third spoke"), asked)

    decision = TurnPolicy(
        _config(), stop_guards=(passing, speaking, never)
    ).decide_after_step(TurnSnapshot(iteration=1, mode="plan"))

    assert (decision.action, decision.reason, decision.inject) == (
        TurnAction.NUDGE,
        "second spoke",
        "补交",
    )
    assert asked == ["first", "second"]  # the first verdict wins; the third never runs


def test_custom_stop_guards_replace_the_plan_submission_rule():
    asked: list[str] = []
    guard = _ScriptedGuard(
        "only", TurnDecision(TurnAction.FINISH, "custom verdict"), asked
    )
    decision = TurnPolicy(_config(), stop_guards=(guard,)).decide_after_step(
        # plan_submitted stays False: the default rule would nudge here.
        TurnSnapshot(iteration=1, mode="plan")
    )
    assert (decision.action, decision.reason) == (TurnAction.FINISH, "custom verdict")
    assert asked == ["only"]


@pytest.mark.parametrize("posture", [{"mode": "plan"}, {"readonly": True}])
def test_after_step_silences_the_acceptance_gate_under_a_posture(posture):
    from ai_coding.domain.acceptance import AcceptanceState

    failing = (AcceptanceState(id="AC-1", passed=False),)
    decision = TurnPolicy(_config()).decide_after_step(
        # stage 4 r2: with a plan already in, the acceptance ladder stays
        # silent under the posture (the original intent); an unsubmitted
        # plan is the guard's business, not the acceptance gate's.
        TurnSnapshot(iteration=2, acceptance=failing, plan_submitted=True, **posture)
    )
    assert (decision.action, decision.reason) == (TurnAction.FINISH, "model finished")


def test_the_strategy_ladder_still_fires_under_plan_mode():
    """Strategy/fatigue are PRE gates — the POST early exit cannot reach them."""
    policy = TurnPolicy(_config(stall_iterations=3))
    decision = policy.decide_next_iteration(
        TurnSnapshot(iteration=5, attempted_calls=5, successful_calls=0, mode="plan")
    )
    assert decision.action is TurnAction.NUDGE
    assert (decision.intervention, decision.level) == ("strategy", 1)


# ---- task injection --------------------------------------------------------


def test_inject_task_message_mode_appends_a_volatile_user_block():
    policy = TurnPolicy(_config(task_injection="message"))
    task = TaskState(goal="ship it", items=[_item("1")])

    injection = policy.inject_task(task, "SYSTEM")

    assert isinstance(injection, TaskInjection)
    assert injection.instructions == "SYSTEM"  # the system prompt is untouched
    assert injection.message is not None
    assert injection.message.role == "user"
    assert injection.message.content is not None
    assert injection.message.content.startswith(TASK_MESSAGE_LEAD)
    assert "1. do 1" in injection.message.content
    assert "<task-state>" in injection.message.content


def test_inject_task_system_mode_merges_into_the_instructions():
    policy = TurnPolicy(_config(task_injection="system"))
    task = TaskState(goal="ship it", items=[_item("1")])

    injection = policy.inject_task(task, "SYSTEM")

    assert injection.message is None
    assert injection.instructions is not None
    assert injection.instructions.startswith("SYSTEM")
    assert "1. do 1" in injection.instructions
    assert "<task-state>" in injection.instructions


def test_inject_task_system_mode_without_instructions_uses_the_block_alone():
    policy = TurnPolicy(_config(task_injection="system"))

    injection = policy.inject_task(TaskState(goal="g"), None)

    assert injection.message is None
    assert injection.instructions is not None
    assert "<task-state>" in injection.instructions
    assert "(无任务项)" in injection.instructions  # nothing to list yet


def test_nudge_message_lists_at_most_ten_items():
    items = tuple(_item(str(index)) for index in range(1, 13))

    decision = interventions.select(
        TurnSnapshot(iteration=1, open_items=items),
        phase=interventions.POST,
        limits=interventions.LadderLimits(),
    )
    message = decision.text

    assert "1. do 1" in message
    assert "10. do 10" in message
    assert "11. do 11" not in message


# ---- configuration ---------------------------------------------------------


def test_from_ai_config_reads_the_orchestration_settings():
    settings = AIConfig(
        max_tool_iterations=7, stop_policy="strict", max_todo_nudges=1, task_injection="system"
    )

    config = TurnPolicyConfig.from_ai_config(settings)

    assert config == TurnPolicyConfig(
        max_iterations=7, stop_policy="strict", max_todo_nudges=1, task_injection="system"
    )


def test_from_ai_config_clamps_a_zero_iteration_budget():
    assert TurnPolicyConfig.from_ai_config(AIConfig(max_tool_iterations=0)).max_iterations == 1


# ---- G8-1: the model is told when the budget is nearly gone ----------------


def test_no_pressure_while_the_budget_is_healthy():
    policy = TurnPolicy(TurnPolicyConfig(max_iterations=10))

    assert policy.budget_pressure(iteration=2, tokens_spent=0) is None


def test_pressure_appears_once_the_iterations_run_low():
    """The model cannot see any of these counters; without a word it discovers
    the cap only by being cut off mid-plan."""
    policy = TurnPolicy(TurnPolicyConfig(max_iterations=10))

    text = policy.budget_pressure(iteration=9, tokens_spent=0)

    assert text is not None
    assert "9/10" in text
    assert "收尾" in text


def test_pressure_offers_an_out_when_the_evidence_is_not_enough():
    """RC-5: "不要再开新的工作" plus a task that asks for one deep answer made
    the cheapest satisfying move "fill the gap from memory".  Measured on T2:
    the block appeared at iteration 32, the model read at 34, and answered at
    35 -- with a constructor it had never opened."""
    policy = TurnPolicy(TurnPolicyConfig(max_iterations=10))

    text = policy.budget_pressure(iteration=9, tokens_spent=0)

    assert "未确认" in text


def test_pressure_appears_once_the_token_budget_runs_low():
    policy = TurnPolicy(TurnPolicyConfig(max_iterations=10, max_turn_tokens=10_000))

    text = policy.budget_pressure(iteration=1, tokens_spent=9_000)

    assert text is not None
    assert "10,000" in text or "10000" in text


def test_an_unlimited_token_budget_says_nothing_about_tokens():
    """``0`` means "no ceiling", and a warning about a ceiling that does not
    exist would be a lie the model acts on."""
    policy = TurnPolicy(TurnPolicyConfig(max_iterations=100, max_turn_tokens=0))

    assert policy.budget_pressure(iteration=1, tokens_spent=10**9) is None


def test_the_last_iteration_is_not_announced_as_available():
    """Pressure means "wrap up now", not "you have another round" -- firing at
    the last allowed iteration is exactly when it is most needed."""
    policy = TurnPolicy(TurnPolicyConfig(max_iterations=10))

    assert policy.budget_pressure(iteration=10, tokens_spent=0) is not None
    assert policy.budget_pressure(iteration=11, tokens_spent=0) is None


# ---- 13 §2.1: the four gates now run through the ladder (select) -----------


def test_next_iteration_runs_the_ladder_instead_of_finishing_on_stall():
    """Sanctioned change ①: stall used to FINISH immediately; it now starts the
    strategy ladder at L1 (13 §2.1)."""
    policy = TurnPolicy(_config(stall_iterations=3))
    snap = TurnSnapshot(iteration=5, attempted_calls=5, successful_calls=0)

    decision = policy.decide_next_iteration(snap)

    assert decision.action is TurnAction.NUDGE
    assert (decision.intervention, decision.level) == ("strategy", 1)


def test_after_step_escalates_to_the_ask_level_when_the_budget_is_spent():
    policy = TurnPolicy(_config(max_todo_nudges=2))
    snap = TurnSnapshot(
        iteration=4, open_items=(_item("1"),), nudges=2,
        intervention_ledger={"task": {"level": 1, "at": [2, 4], "outcome": "pending",
                                      "fires": 2, "ref": {}}},
    )

    decision = policy.decide_after_step(snap)

    assert decision.action is TurnAction.NUDGE
    assert (decision.intervention, decision.level) == ("task", 2)


def test_after_step_finishes_when_the_ask_was_ignored():
    policy = TurnPolicy(_config())
    snap = TurnSnapshot(
        iteration=6, open_items=(_item("1"),),
        intervention_ledger={"task": {"level": 2, "at": [4, 6], "outcome": "pending",
                                      "fires": 1, "ref": {}}},
    )

    decision = policy.decide_after_step(snap)

    assert (decision.action, decision.reason) == (
        TurnAction.FINISH, "todo ladder exhausted"
    )


def test_the_guard_lets_an_approved_plan_through():
    """占位 = 成功: an approval is the plan exit's success, so the guard must
    not ask for a submission the human already gave."""
    decision = TurnPolicy(_config()).decide_after_step(
        TurnSnapshot(iteration=1, mode="plan", plan_approved=True)
    )
    assert (decision.action, decision.reason) == (TurnAction.FINISH, "model finished")
