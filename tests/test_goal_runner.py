"""GoalRunner (D-4): the self-driving goal profile over TaskDriver.

Driven by a scripted fake loop: each round's effect on the task is configured
per call, so the control flow is pinned without any model — the round prompt's
objective and round numbers, the episode-wide cap, the two model claims, the
no-progress guard, a mid-run disarm, C3 being off inside a round and restored
after, and the arm/disarm + persist lifecycle (crash point ②).
"""

from types import SimpleNamespace
from typing import Any

import pytest

from ai_coding.core.goal_runner import GoalRunner, goal_round_prompt
from ai_coding.core.turn_policy import TurnPolicy
from ai_coding.domain.task import TaskState


class _Closer:
    def __init__(self) -> None:
        self.saves = 0

    def save(self, session: Any) -> None:
        self.saves += 1


class _FakeLoop:
    def __init__(
        self,
        *,
        effects: list[Any] | None = None,
        tools: bool = True,
        cap: int = 20,
        budget: int = 0,
        usage: dict | None = None,
    ) -> None:
        self.policy = TurnPolicy()
        self.closer = _Closer()
        self.settings = SimpleNamespace(goal_max_rounds=cap, goal_token_budget=budget)
        self.calls: list[str] = []
        self.effects = list(effects or [])
        self.tools = tools
        #: What the provider "reported" for each round's turn; None = no report
        #: (the fail-open case).  Set as ``last_outcome.usage`` after the turn,
        #: exactly where the real loop leaves it (turn_close.py:218).
        self.usage = usage
        #: C3's budget as each round's turn saw it (must be 0).
        self.policies_seen: list[int] = []

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        index = len(self.calls)
        self.calls.append(text)
        self.policies_seen.append(self.policy.config.max_auto_continuations)
        from ai_coding.domain.message import ChatMessage

        stamp = "2026-01-01T00:00:00Z"
        session.messages.append(ChatMessage(role="user", content=text, timestamp=stamp))
        if self.tools:
            session.messages.append(
                ChatMessage(role="tool", content="ok", timestamp=stamp, tool_call_id="c1")
            )
        session.messages.append(ChatMessage(role="assistant", content="done", timestamp=stamp))
        if index < len(self.effects) and self.effects[index] is not None:
            self.effects[index](session, session.task)
        if self.usage is not None:
            self.last_outcome = SimpleNamespace(usage=dict(self.usage))
        return "done"


def _session(task: TaskState | None) -> SimpleNamespace:
    return SimpleNamespace(task=task, messages=[], session_id="s", open_turn=None)


def _claim(phase: str):
    def effect(session, task):
        task.goal_phase = phase

    return effect


def _disarm(session, task):
    task.armed = False


@pytest.mark.asyncio
async def test_rounds_advance_until_the_model_completes():
    task = TaskState(goal="把 jest 迁到 vitest")
    task.add("先跑通 baseline")  # items are optional context, not the unit
    loop = _FakeLoop(effects=[None, _claim("complete")], cap=3)

    report = await GoalRunner(loop).run(_session(task))

    assert loop.calls[0].startswith("[goal] 第 1/3 轮")
    assert "把 jest 迁到 vitest" in loop.calls[0]
    assert loop.calls[1].startswith("[goal] 第 2/3 轮")
    assert len(loop.calls) == 2
    assert report.rounds == 2 and report.stop == "model-complete"
    assert task.rounds_run == 2
    assert task.armed is False  # every exit disarms
    assert loop.closer.saves >= 3  # arm + per-round + disarm


@pytest.mark.asyncio
async def test_cap_is_episode_wide_and_budget_stop_says_what_was_left():
    task = TaskState(goal="无底洞")
    loop = _FakeLoop(cap=3)

    report = await GoalRunner(loop).run(_session(task))

    assert len(loop.calls) == 3
    assert report.rounds == 3 and report.stop == "budget"
    assert task.rounds_run == 3
    assert task.armed is False


@pytest.mark.asyncio
async def test_a_resumed_episode_keeps_counting_rounds_toward_the_cap():
    """Boot resume: ``roundsRun`` is on disk, so the cap is episode-wide."""
    task = TaskState(goal="g", rounds_run=2)
    loop = _FakeLoop(cap=3)

    report = await GoalRunner(loop).run(_session(task))

    assert loop.calls[0].startswith("[goal] 第 3/3 轮")
    assert len(loop.calls) == 1
    assert report.stop == "budget"


@pytest.mark.asyncio
async def test_a_round_with_no_tool_call_stops_as_no_progress():
    """Codex's no-progress guard: a talking-only round must not spin a loop."""
    task = TaskState(goal="g")
    loop = _FakeLoop(tools=False, cap=5)

    report = await GoalRunner(loop).run(_session(task))

    assert len(loop.calls) == 1
    assert report.rounds == 1 and report.stop == "no-progress"
    assert task.armed is False


@pytest.mark.asyncio
async def test_a_blocked_claim_stops_the_run():
    task = TaskState(goal="g")
    loop = _FakeLoop(effects=[None, None, _claim("blocked")], cap=5)

    report = await GoalRunner(loop).run(_session(task))

    assert report.rounds == 3 and report.stop == "model-blocked"
    assert task.rounds_run == 3
    assert task.armed is False


@pytest.mark.asyncio
async def test_a_mid_run_disarm_stops_before_the_next_round():
    task = TaskState(goal="g")
    loop = _FakeLoop(effects=[_disarm, None], cap=5)

    report = await GoalRunner(loop).run(_session(task))

    assert len(loop.calls) == 1
    assert report.rounds == 1 and report.stop == "disarmed"
    assert task.armed is False


@pytest.mark.asyncio
async def test_no_task_is_a_clean_noop():
    loop = _FakeLoop()

    report = await GoalRunner(loop).run(_session(None))

    assert report.rounds == 0 and report.stop == "no-task"
    assert loop.calls == []
    assert loop.closer.saves == 0


@pytest.mark.asyncio
async def test_the_round_turn_runs_with_c3_disabled_and_the_policy_is_restored():
    """The driver owns cross-round continuation; C3 inside a round would
    blur the round boundary (and its persistence point)."""
    task = TaskState(goal="g")
    loop = _FakeLoop(effects=[_claim("complete")], cap=3)
    original = loop.policy

    await GoalRunner(loop).run(_session(task))

    assert loop.policies_seen == [0]
    assert loop.policy is original


def test_the_round_prompt_carries_the_objective_round_and_claim_rules():
    text = goal_round_prompt("迁移 X", 2, 7)
    assert text.startswith("[goal] 第 2/7 轮")
    assert "迁移 X" in text
    assert "update_goal(complete)" in text
    assert "update_goal(blocked" in text


# ---- G1: the episode's token budget (2026-10-17) -----------------------------
#
# Codex tracks tokens per goal and stops at the budget; our v1 filed this for
# v2 with the reason on record.  The accounting is episode-wide and persisted
# (the same shape as roundsRun): a crash-resume continues the same budget
# instead of letting the breaker (x5 resumes) multiply it.


@pytest.mark.asyncio
async def test_the_token_budget_stops_after_the_round_that_crosses_it():
    task = TaskState(goal="g")
    loop = _FakeLoop(cap=5, budget=1000, usage={"total_tokens": 600})

    report = await GoalRunner(loop).run(_session(task))

    assert report.rounds == 2, "600 < 1000 after round 1; 1200 crosses it in round 2"
    assert report.stop == "token-budget"
    assert task.tokens_used == 1200, "the episode's account, on the task"
    assert task.armed is False
    text = report.render()
    assert "已用 1,200 tokens" in text
    assert "token 预算用尽（1,200/1,000）" in text


@pytest.mark.asyncio
async def test_a_token_budget_of_zero_never_fires():
    task = TaskState(goal="g")
    loop = _FakeLoop(
        cap=3, effects=[None, _claim("complete")], budget=0,
        usage={"total_tokens": 10_000_000},
    )

    report = await GoalRunner(loop).run(_session(task))

    assert report.stop == "model-complete"
    assert task.tokens_used == 20_000_000, "the counter runs even with the cap off"
    assert "token 预算" not in report.render()


@pytest.mark.asyncio
async def test_unknown_usage_is_not_charged():
    task = TaskState(goal="g")
    loop = _FakeLoop(cap=3, effects=[None, _claim("complete")], budget=10, usage=None)

    report = await GoalRunner(loop).run(_session(task))

    assert report.stop == "model-complete", "an unreported turn must not trip the cap"
    assert task.tokens_used == 0
    assert "已用" not in report.render()


@pytest.mark.asyncio
async def test_the_round_prompt_carries_the_budget_line_only_when_set():
    task = TaskState(goal="g")
    loop = _FakeLoop(cap=5, budget=1000, usage={"total_tokens": 600})
    await GoalRunner(loop).run(_session(task))
    assert "token 预算：已用 0/1,000" in loop.calls[0]
    assert "token 预算：已用 600/1,000" in loop.calls[1]

    other = _FakeLoop(cap=3, effects=[None, _claim("complete")], budget=0)
    await GoalRunner(other).run(_session(TaskState(goal="g")))
    assert all("token 预算" not in call for call in other.calls)


@pytest.mark.asyncio
async def test_a_resumed_episode_keeps_counting_tokens_toward_the_budget():
    task = TaskState(goal="g", tokens_used=1500)
    loop = _FakeLoop(cap=5, budget=1000, usage={"total_tokens": 600})

    report = await GoalRunner(loop).run(_session(task))

    assert report.rounds == 1, "the resume runs one round, then the account stops it"
    assert report.stop == "token-budget"
    assert task.tokens_used == 2100


@pytest.mark.asyncio
async def test_a_goal_run_emits_task_updated_on_arm_rounds_and_disarm():
    """G2: the sidebar's goal line rides TaskUpdated -- arm, every round, and
    the disarm, all on the event todo_write already uses.

    The event carries the live task object (the loop's own convention; the
    bridge consumes it synchronously), so this double snapshots the fields at
    call time instead of holding the reference.
    """
    from ai_coding.core.events import TaskUpdated

    class _Snapshot:
        def __init__(self) -> None:
            self.states: list[tuple[bool, int, int]] = []

        def on_event(self, event) -> None:
            if isinstance(event, TaskUpdated):
                self.states.append(
                    (event.task.armed, event.task.rounds_run, event.task.tokens_used)
                )

    task = TaskState(goal="g")
    loop = _FakeLoop(cap=3, usage={"total_tokens": 600})
    observer = _Snapshot()

    report = await GoalRunner(loop).run(_session(task), observer=observer)

    assert len(observer.states) >= 3, f"arm + rounds + disarm, got {len(observer.states)}"
    assert observer.states[0][0] is True, "the arm emission says armed"
    assert observer.states[-1][0] is False, "the last emission says disarmed"
    rounds = [state[1] for state in observer.states]
    assert rounds == sorted(rounds), "the numbers only grow"
    assert rounds[-1] == report.rounds
    assert observer.states[-1][2] == report.tokens_used
