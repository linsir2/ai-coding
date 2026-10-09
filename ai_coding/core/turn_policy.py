"""Turn policy — task injection and the stop decision, as a pure unit (R2c).

Extracted from ``core/agent_loop.py`` (its fan-out was 21, the highest in the
project, and this was the part that had to be read as a whole).  Everything here
is pure: no IO, no asyncio, no events, no persistence — so the decision rules can
be enumerated in a table test (``tests/test_turn_policy.py``).

The loop keeps the choreography (engine step, dispatch, checkpoint, events); this
unit answers two questions:

* **may another iteration start**, given the iteration budget and the turn clock?
* **how does a finished model step end**, given the task list and the nudge budget?

It also owns the two pieces of prompt text the task list needs: the volatile
``<task-state>`` block that is attached to a request only (never persisted) and
the persisted ``[task-gate]`` nudge (docs/r1a-4-analysis.md D1).
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai_coding.domain.acceptance import AcceptanceState
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.task import TaskState, TodoItem

__all__ = [
    "NUDGE_ITEM_LIMIT",
    "STRATEGY_GATE_LEAD",
    "TASK_GATE_LEAD",
    "TASK_GATE_TAIL",
    "TASK_MESSAGE_LEAD",
    "TaskInjection",
    "TurnAction",
    "TurnDecision",
    "TurnPolicy",
    "TurnPolicyConfig",
    "TurnSnapshot",
]

TASK_MESSAGE_LEAD = "以下是你自己的任务清单状态（系统注入，不是用户发言）："
TASK_GATE_LEAD = "[task-gate] 你还有未完成的任务项："
TASK_GATE_TAIL = "请继续推进，或把不再需要的条目用 todo_write 标记为 dropped 并说明原因。"
STRATEGY_GATE_LEAD = "[strategy-gate]"
#: The acceptance gate's marker (R0-1): failing proof commands, persisted
#: like the other gates because they changed what the model did next.
ACCEPTANCE_GATE_LEAD = "[acceptance-gate]"
NUDGE_ITEM_LIMIT = 10
#: G8-1: the marker for the one block that tells the model its budget is nearly
#: gone.  Volatile (never persisted) like the memory and task blocks -- it
#: describes a counter, not a fact about the work.
BUDGET_PRESSURE_LEAD = "[budget] 本轮预算快用完了"
#: How much of a budget may remain before the warning appears.
PRESSURE_RATIO = 0.2
#: F2: fatigue gate marker.
FATIGUE_GATE_LEAD = "[fatigue-gate]"
#: F2: Paris' Law fatigue threshold — accumulated error damage that triggers
#: the fatigue gate.
FATIGUE_THRESHOLD = 30.0


@dataclass(frozen=True)
class TurnPolicyConfig:
    """The orchestration settings this unit needs, read once per loop."""

    max_iterations: int = 10
    stop_policy: str = "todo_aware"  # natural | todo_aware | strict
    max_todo_nudges: int = 2
    task_injection: str = "message"  # message | system
    # R4b: cumulative tokens one turn may spend (0 = no ceiling).  It counts the
    # re-sent context of every step, so it is a spend cap, not a window size.
    max_turn_tokens: int = 0
    # R6b: the escalation ladder.  Repeated failures buy exactly one "change
    # strategy" nudge; a turn in which nothing has ever succeeded stops early so
    # the handoff can report it (docs/r6-analysis.md 2.2).
    failed_streak_threshold: int = 2  # 0 disables the strategy nudge
    max_strategy_nudges: int = 1
    stall_iterations: int = 3  # 0 disables the early stop
    # R0-1: the acceptance gate's own budget, separate from the todo and
    # strategy ladders — a turn that keeps failing its proof commands stops
    # asking instead of re-running the suite forever.
    max_acceptance_nudges: int = 2
    max_auto_continuations: int = 3

    @classmethod
    def from_ai_config(cls, settings: Any) -> TurnPolicyConfig:
        """Build from ``AIConfig`` (duck-typed: fakes in tests work too)."""
        return cls(
            max_iterations=max(1, int(getattr(settings, "max_tool_iterations", 10))),
            stop_policy=str(getattr(settings, "stop_policy", "todo_aware")),
            max_todo_nudges=int(getattr(settings, "max_todo_nudges", 2)),
            task_injection=str(getattr(settings, "task_injection", "message")),
            max_turn_tokens=int(getattr(settings, "turn_token_budget", 0) or 0),
            failed_streak_threshold=int(
                getattr(settings, "failed_streak_threshold", 2) or 0
            ),
            max_strategy_nudges=int(getattr(settings, "max_strategy_nudges", 1) or 0),
            stall_iterations=int(getattr(settings, "stall_iterations", 3) or 0),
            max_acceptance_nudges=int(getattr(settings, "max_acceptance_nudges", 2) or 0),
            max_auto_continuations=int(
                getattr(settings, "max_auto_continuations", 3) or 0
            ),
        )


class TurnAction(enum.Enum):
    """What the loop should do after a decision."""

    ITERATE = "iterate"  # start another model step
    NUDGE = "nudge"  # inject ``decision.inject`` and iterate again
    FINISH = "finish"  # the turn is over


@dataclass(frozen=True)
class TurnDecision:
    """One policy verdict, with the reason the loop records in its outcome."""

    action: TurnAction
    reason: str = ""
    inject: str | None = None


@dataclass(frozen=True)
class TurnSnapshot:
    """The observable state a decision depends on."""

    iteration: int  # the iteration about to start, or the one just finished
    nudges: int = 0
    open_items: tuple[TodoItem, ...] = ()
    final_text: str | None = None
    tokens_spent: int = 0  # R4b: what this turn has spent so far
    # C3: continuation rounds already spent this turn (budget died with items
    # open, the loop started a fresh round).  Kept on the session so it
    # survives across the rounds — in-process only; the session's to_dict
    # whitelist does not persist it, and a new user turn resets it.
    continuations: int = 0
    # R6b: how the tool calls have been going.  Fed in by the loop — the policy
    # never counts anything itself, so it stays pure and table-testable.
    failed_streak: int = 0  # consecutive iterations where every call failed
    attempted_calls: int = 0  # tool calls this turn (successful or not)
    successful_calls: int = 0  # successful tool calls this turn
    strategy_nudges: int = 0  # strategy nudges already injected this turn
    # R0-1: acceptance verdicts, fed in by the loop after running the proof
    # commands — the policy never runs anything itself.  Empty = no contracts,
    # and every rule below is skipped, so turns without acceptance behave
    # exactly as before.
    acceptance: tuple[AcceptanceState, ...] = ()
    acceptance_nudges: int = 0  # acceptance nudges already injected this turn
    # F2: Paris' Law fatigue index — accumulated error damage.
    fatigue_index: float = 0.0
    # F2: fatigue nudges already injected this turn.
    fatigue_nudges: int = 0


@dataclass(frozen=True)
class TaskInjection:
    """Where the task list went: the system prompt, or a trailing user block."""

    instructions: str | None = None
    message: ChatMessage | None = None


class TurnPolicy:
    """Pure decision unit: budgets in, action out."""

    def __init__(self, config: TurnPolicyConfig | None = None) -> None:
        self.config = config or TurnPolicyConfig()

    # ---- budget ------------------------------------------------------------

    def decide_next_iteration(
        self, snapshot: TurnSnapshot, *, budget_expired: bool = False
    ) -> TurnDecision:
        """May ``snapshot.iteration`` start?  The turn clock wins over the count."""
        if budget_expired:
            return TurnDecision(TurnAction.FINISH, "turn deadline exceeded")
        # M1 audit fix: token exhaustion must not burn a C3 continuation.  The
        # gate is computed once and guards the continuation below; the reason
        # precedence for the plain ladder (iteration first) is unchanged.
        token_gate = bool(self.config.max_turn_tokens) and (
            snapshot.tokens_spent >= self.config.max_turn_tokens
        )
        if snapshot.iteration > self.config.max_iterations:
            # C3: the budget is gone but the work is not — open items mean the
            # model never finished, so inject a continuation prompt and start a
            # fresh round rather than handing the user a dead turn.  Fires
            # regardless of stop_policy: unfinished work is not a stop-policy
            # question.  The cap keeps a model that never closes its items from
            # running forever; an exhausted token budget makes the continuation
            # pointless, so it falls through to FINISH instead.
            # A blocked item is open but not continuable: it says this step
            # cannot be finished now (the model gave up, or the verify gate
            # ruled it out), which is the opposite of C3's premise — the model
            # stopped mid-way through its own work.  The runner still retries
            # those steps on the next /plan; they just must not cost this
            # turn extra rounds (measured: 6 model calls and 3 continuations
            # for a single blocked item, identical to a pending one).
            actionable = tuple(
                item for item in snapshot.open_items if item.status != "blocked"
            )
            if (
                actionable
                and snapshot.continuations < self.config.max_auto_continuations
                and not token_gate
            ):
                return TurnDecision(
                    TurnAction.NUDGE,
                    "continue unfinished work",
                    inject=self.continuation_message(actionable),
                )
            return TurnDecision(TurnAction.FINISH, "iteration budget exhausted")
        if token_gate:
            return TurnDecision(TurnAction.FINISH, "turn token budget exhausted")
        # R6b tier 3: the turn keeps *calling tools* and none of them ever works,
        # so another round is not going to start working — stop while there is
        # budget left to report (the handoff is what the user gets instead of
        # silence: docs/r6-analysis.md 1.3).  ``attempted_calls`` is what keeps
        # this from firing on a turn that simply talks: a model that never calls a
        # tool is governed by the stop policy, not by this ladder — the `strict`
        # todo policy relies on exactly that (tests/test_agent_loop_react.py).
        stall = self.config.stall_iterations
        if (
            stall
            and snapshot.iteration > stall
            and snapshot.attempted_calls
            and snapshot.successful_calls == 0
        ):
            return TurnDecision(
                TurnAction.FINISH,
                f"no progress: no tool call has succeeded in "
                f"{snapshot.iteration - 1} iterations",
            )
        # F2: fatigue gate — accumulated errors beyond threshold buy one chance
        # to change approach or summarize progress.
        if (
            snapshot.fatigue_index >= FATIGUE_THRESHOLD
            and snapshot.fatigue_nudges < 1
        ):
            return TurnDecision(
                TurnAction.NUDGE,
                "accumulated errors (fatigue)",
                inject=self.fatigue_message(snapshot.fatigue_index),
            )
        # R6b tier 2: repeated failures buy one chance to change approach.
        threshold = self.config.failed_streak_threshold
        if (
            threshold
            and snapshot.failed_streak >= threshold
            and snapshot.strategy_nudges < self.config.max_strategy_nudges
        ):
            return TurnDecision(
                TurnAction.NUDGE,
                "repeated tool failures",
                inject=self.strategy_message(snapshot.failed_streak),
            )
        return TurnDecision(TurnAction.ITERATE, "within budget")

    @staticmethod
    def strategy_message(failed_streak: int) -> str:
        """The one nudge a failing turn gets before it is allowed to stop (R6b)."""
        return "\n".join(
            [
                f"{STRATEGY_GATE_LEAD} 最近 {failed_streak} 轮的工具调用全部失败。"
                "不要重复同样的做法：",
                "1) 先读错误输出，说清直接原因，不要猜；",
                "2) 换手段或把目标切小（换工具、换路径、先写最小复现）；",
                "3) 若确实缺信息或需要人拍板，用 ask_user 提问；"
                "没有人回答时就给出结论与假设后收尾。",
            ]
        )

    @staticmethod
    def fatigue_message(fatigue_index: float) -> str:
        """The nudge when accumulated errors cross the fatigue threshold."""
        return "\n".join(
            [
                f"{FATIGUE_GATE_LEAD} 错误累积过高（fatigue index: {fatigue_index:.1f}）。"
                "不要继续当前路径：",
                "1) 停下来，总结你当前的理解和已取得的进展；",
                "2) 换一种完全不同的方法；",
                "3) 如果确实需要更多信息，用 ask_user 提问。",
            ]
        )

    # ---- stopping ----------------------------------------------------------

    def decide_after_step(self, snapshot: TurnSnapshot) -> TurnDecision:
        """The model stopped asking for tools: finish, or gate on open items?"""
        if snapshot.acceptance:
            # R0-1: a turn with contracts does not end on the model's word.
            # Unchecked items fail closed (the loop verifies before asking, so
            # ``None`` here means "could not be checked" — never "passed").
            failed = [a for a in snapshot.acceptance if a.passed is not True]
            if not failed:
                return TurnDecision(TurnAction.FINISH, "acceptance verified")
            if snapshot.acceptance_nudges >= self.config.max_acceptance_nudges:
                return TurnDecision(
                    TurnAction.FINISH, "acceptance nudge budget exhausted"
                )
            return TurnDecision(
                TurnAction.NUDGE,
                "acceptance checks failing",
                inject=self.acceptance_message(snapshot.acceptance),
            )
        if not snapshot.open_items or self.config.stop_policy == "natural":
            return TurnDecision(TurnAction.FINISH, "model finished")
        if (
            self.config.stop_policy != "strict"
            and snapshot.nudges >= self.config.max_todo_nudges
        ):
            # "strict" never gives up early: the iteration/clock budget is its
            # only bound (docs/r1-analysis.md D5).
            return TurnDecision(TurnAction.FINISH, "todo nudge budget exhausted")
        return TurnDecision(
            TurnAction.NUDGE,
            "open todo items remain",
            inject=self.nudge_message(snapshot.open_items),
        )

    # ---- task text ---------------------------------------------------------

    def inject_task(self, task: TaskState, instructions: str | None) -> TaskInjection:
        """Attach the task list to this request only (never to the session)."""
        render = task.render()
        if self.config.task_injection == "system":
            merged = f"{instructions}\n\n{render}" if instructions else render
            return TaskInjection(instructions=merged)
        block = ChatMessage(
            role="user", content=f"{TASK_MESSAGE_LEAD}\n{render}", timestamp=""
        )
        return TaskInjection(instructions=instructions, message=block)

    def budget_pressure(self, *, iteration: int, tokens_spent: int) -> str | None:
        """A note to the model when the turn's budget is nearly gone (G8-1).

        Six separate budgets govern a turn and the model can see none of them --
        it discovers the cap by being cut off mid-plan, which is the one moment
        the work is least resumable.  This is the harness saying so while there
        is still a round left to spend on wrapping up.

        Pure and side-effect free: it reads the config it was built with and two
        numbers the caller already has.  No tokens of its own, no extra call.

        Returns ``None`` when there is nothing true to say -- a healthy budget,
        a turn that is already past its cap (the decision ladder owns that), or
        an unlimited token budget, where warning about a ceiling that does not
        exist would be a lie the model acts on.
        """
        parts: list[str] = []
        if 0 < iteration <= self.config.max_iterations:
            remaining = self.config.max_iterations - iteration
            if remaining <= max(1, int(self.config.max_iterations * PRESSURE_RATIO)):
                parts.append(f"步数 {iteration}/{self.config.max_iterations}")
        limit = self.config.max_turn_tokens
        if limit > 0:
            remaining_tokens = limit - tokens_spent
            if remaining_tokens <= int(limit * PRESSURE_RATIO):
                parts.append(f"token {tokens_spent:,}/{limit:,}")
        if not parts:
            return None
        return "\n".join(
            [
                f"{BUDGET_PRESSURE_LEAD}（{'，'.join(parts)}）。",
                "请用这一轮收尾：把已完成的和还差的讲清楚，不要再开新的工作。",
            ]
        )

    @staticmethod
    def continuation_message(open_items: Sequence[TodoItem]) -> str:
        """C3's continuation prompt: the budget died, the work did not.

        Names what is still open so the next round knows exactly what to pick
        up, and says not to redo finished work — the same discipline as the
        ledger gate's nudge.
        """
        lines = [
            "[continue] 上一轮预算耗尽，但还有未完成的任务项："
        ]
        lines.extend(
            f"- {item.id}. {item.content}" for item in open_items[:NUDGE_ITEM_LIMIT]
        )
        lines.append("继续未完成的工作，不要重复已完成的部分。")
        return "\n".join(lines)

    @staticmethod
    def nudge_message(open_items: Sequence[TodoItem]) -> str:
        lines = [TASK_GATE_LEAD]
        lines.extend(
            f"- {item.id}. {item.content}" for item in open_items[:NUDGE_ITEM_LIMIT]
        )
        lines.append(TASK_GATE_TAIL)
        return "\n".join(lines)

    @staticmethod
    def acceptance_message(states: Sequence[AcceptanceState]) -> str:
        """The acceptance gate's nudge: which checks failed, and their output.

        Quoting the tails is what keeps the next iteration from re-running the
        suite blind — the evidence travels with the verdict.
        """
        lines = [f"{ACCEPTANCE_GATE_LEAD} 以下验收项未通过："]
        for state in states[:NUDGE_ITEM_LIMIT]:
            mark = "未检查" if state.passed is None else "未通过"
            lines.append(f"- {state.id}（{mark}）")
            if state.output:
                lines.append(f"  输出：{state.output}")
        lines.append("请继续推进；修正后验收会自动重跑，无需手动重验。")
        return "\n".join(lines)
