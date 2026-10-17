"""Goal runner (D-4): a self-declared objective drives itself round by round.

The plan profile executes a human-approved plan whose steps carry proof
commands; this profile drives a goal the **model** started itself
(``start_goal``) or the human set (``/goal``) — no planning gate, no proof
commands.  Each round is one ordinary turn on the *main* session, reopened
from the round boundary (persisted ``roundsRun``), so the conversation is the
continuity and steering/inbox input is absorbed at each round's turn entry.

Stop conditions, one line each:

* ``model-complete`` / ``model-blocked`` — the model's own claim
  (``update_goal``); the driver only reads it after the round, so the claim
  never cuts a turn in half.
* ``budget`` — the episode-wide round cap (``goal_max_rounds``); counting is
  episode-wide so a crash-resume continues the same budget (dsh counts
  ``roundsStarted`` on the goal, not per process).
* ``no-progress`` — a round whose turn made no tool call at all (Codex's
  no-progress guard: "if a continuation turn makes no tool call, the next
  automatic continuation is suppressed so Codex does not spin").
* ``disarmed`` — Esc / ``/goal off`` somewhere mid-run; the driver re-checks
  ``armed`` every round (the T7 seam).
* ``no-task`` — nothing to drive.

And every exit disarms + persists, exactly like the plan run (one lifecycle
for both profiles; the boot scan and the breaker see the same shapes).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from ai_coding.core.task_driver import TaskDriver

__all__ = ["GOAL_ROUND_HEAD", "GoalReport", "GoalRunner", "goal_round_prompt"]

#: The round prompt's leading marker.  No replay machinery keys on it (goal
#: rounds run on the main session, so their bubbles arrive in order like any
#: other turn); it exists so the user can tell a driver round from their own
#: message at a glance.
GOAL_ROUND_HEAD = "[goal] 第"

#: Settings default, read duck-typed like the plan runner's cap.
DEFAULT_MAX_ROUNDS = 20


def goal_round_prompt(objective: str, number: int, cap: int) -> str:
    """One round's opening message: objective, round, and the doctrine.

    The three sentences are the two reference harnesses' continuation doctrine
    compressed (dsh ``renderGoalRoundPrompt``: workspace authoritative, gather
    evidence before claiming completion; Codex ``continuation.md``: keep the
    full objective intact, never redefine success smaller, only claim what the
    evidence proves).
    """
    return (
        f"{GOAL_ROUND_HEAD} {number}/{cap} 轮 · 目标：{objective}\n"
        "以工作区现状、工具结果与会话状态为准（不要拿早先的叙述当现状）。\n"
        "本轮继续推进真实目标；允许调整做法，不得把目标缩小或换成更省事的版本。"
        "需要时用 todo_write 维护你的步骤清单。\n"
        "结束前自检：完成了吗？只有当前证据逐条满足目标才可 update_goal(complete)；"
        "同一阻塞连续出现 ≥3 轮才可 update_goal(blocked, note)；"
        "否则直接结束本轮，并在回复里说明进展与下一步。"
    )


@dataclass
class _Round:
    """One round's synthetic item — the driver protocol needs an id."""

    id: str
    number: int


@dataclass
class GoalReport:
    """What one goal run did, for the console and the caller."""

    rounds: int = 0
    #: None only before the first verdict; every driver exit fills one.
    stop: str | None = field(default=None)

    _STOP_TEXT: ClassVar[dict[str, str]] = {
        "model-complete": "目标已申报完成",
        "model-blocked": "模型申报受阻（同一阻塞 ≥3 轮）",
        "budget": "轮数用尽（目标未申报完成，清单留在任务里）",
        "no-progress": "无进展（本轮零工具调用）——已停下，等你指示",
        "disarmed": "被停止（Esc / /goal off）",
        "no-task": "没有任务，未立案",
    }

    def render(self) -> str:
        head = f"[goal] 共 {self.rounds} 轮"
        text = self._STOP_TEXT.get(self.stop or "", self.stop or "")
        return f"{head} · 止于：{text}"


class GoalRunner:
    """Drive one armed goal; the ``goal`` RoundProfile (D-4)."""

    def __init__(self, loop: Any) -> None:
        self._loop = loop
        #: Stashed by ``prepare`` for ``should_stop``, whose protocol signature
        #: cannot carry the task (same trick as the plan runner's receipt set).
        self._task: Any = None
        self._cap = DEFAULT_MAX_ROUNDS
        #: Did the round's turn move anything (any tool call at all)?
        self._round_moved = False

    async def run(
        self,
        session: Any,
        console: Any = None,
        *,
        on_delta: Any = None,
        observer: Any = None,
    ) -> GoalReport:
        task = getattr(session, "task", None)
        if task is not None:
            task.armed = True
            task.armed_profile = "goal"
            # Crash point ①: armed is durable before any round runs, exactly
            # like the plan run — the boot scan finds it, the breaker counts.
            self._persist(session)
        try:
            report: GoalReport = await TaskDriver(self._loop).run(
                session, self, console, on_delta=on_delta, observer=observer
            )
        finally:
            if task is not None and getattr(task, "armed", False):
                task.armed = False
                self._persist(session)
        # The driver's armed-break path carries no verdict of its own.
        if report.stop is None:
            report.stop = "disarmed"
        return report

    # ---- RoundProfile ----------------------------------------------------

    def new_report(self) -> GoalReport:
        return GoalReport()

    def max_rounds(self) -> int:
        settings = getattr(self._loop, "settings", None)
        value = getattr(settings, "goal_max_rounds", DEFAULT_MAX_ROUNDS)
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return DEFAULT_MAX_ROUNDS

    def prepare(self, session: Any, task: Any) -> set[str]:
        self._task = task
        self._cap = self.max_rounds()
        return set()

    def next_item(self, task: Any, attempted: set[str]) -> Any:
        """Always the next round — goal work is not item-bound.

        The round number belongs to the episode (``roundsRun`` on disk), so a
        resumed run continues numbering instead of restarting at 1.
        """
        number = int(getattr(task, "rounds_run", 0) or 0) + 1
        return _Round(id=f"r{number}", number=number)

    def no_item(self, task: Any, report: GoalReport) -> None:
        """Unreachable: ``next_item`` never returns ``None``."""

    def rounds_used(self, report: GoalReport) -> int:
        """Episode-wide on purpose: the cap bounds the whole goal, not one process."""
        return int(getattr(self._task, "rounds_run", 0) or 0)

    def budget_stop(self, report: GoalReport, cap: int) -> None:
        report.stop = "budget"

    def before_round(
        self, task: Any, session: Any, report: GoalReport, receipted: set[str]
    ) -> None:
        pass

    async def run_round(
        self,
        session: Any,
        task: Any,
        item: _Round,
        report: GoalReport,
        console: Any,
        *,
        on_delta: Any,
        observer: Any,
    ) -> None:
        goal = str(getattr(task, "goal", "") or "")
        self._say(console, f"[goal] 第 {item.number}/{self._cap} 轮 · {goal}")
        before = self._tool_calls(session)
        original_policy = self._loop.policy
        # C3 off for the round: cross-round continuation is the driver's job,
        # and blurring it would blur the round boundary (and its persistence
        # point) that crash-resume depends on.  Everything else stays the
        # ordinary turn's policy.
        self._loop.policy = type(original_policy)(
            replace(original_policy.config, max_auto_continuations=0)
        )
        try:
            await self._loop.process_input(
                session,
                goal_round_prompt(goal, item.number, self._cap),
                on_delta=on_delta,
                observer=observer,
            )
        finally:
            self._loop.policy = original_policy
        self._round_moved = self._tool_calls(session) > before

    def after_round(self, session: Any, task: Any, report: GoalReport) -> None:
        task.rounds_run = int(getattr(task, "rounds_run", 0) or 0) + 1
        report.rounds += 1
        # Crash point ②: the round number and statuses are on disk before the
        # next round opens.
        self._persist(session)

    def should_stop(self, report: GoalReport, item: _Round) -> bool:
        phase = str(getattr(self._task, "goal_phase", "") or "")
        if phase == "complete":
            report.stop = "model-complete"
            return True
        if phase == "blocked":
            report.stop = "model-blocked"
            return True
        if not self._round_moved:
            report.stop = "no-progress"
            return True
        return False

    def taskless(self, report: GoalReport, console: Any) -> None:
        report.stop = "no-task"
        self._say(console, "[goal] 没有任务（未立案）")

    def persist(self, session: Any) -> None:
        self._persist(session)

    def finish(self, console: Any, report: GoalReport) -> None:
        self._say(console, report.render())

    # ---- internals -------------------------------------------------------

    def _persist(self, session: Any) -> None:
        save = getattr(getattr(self._loop, "closer", None), "save", None)
        if save is not None:
            save(session)

    @staticmethod
    def _tool_calls(session: Any) -> int:
        """How many tool messages the session holds right now.

        Counting messages instead of trusting an outcome field keeps the guard
        working for every loop implementation (fakes, the real one) — and a
        round that ran tools but reports nothing is still progress.
        """
        return sum(
            1
            for message in getattr(session, "messages", ()) or ()
            if getattr(message, "role", "") == "tool"
        )

    @staticmethod
    def _say(console: Any, text: str) -> None:
        if console is not None and text:
            console.info(text)
