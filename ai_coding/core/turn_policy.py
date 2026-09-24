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
NUDGE_ITEM_LIMIT = 10


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
    # R6b: how the tool calls have been going.  Fed in by the loop — the policy
    # never counts anything itself, so it stays pure and table-testable.
    failed_streak: int = 0  # consecutive iterations where every call failed
    attempted_calls: int = 0  # tool calls this turn (successful or not)
    successful_calls: int = 0  # successful tool calls this turn
    strategy_nudges: int = 0  # strategy nudges already injected this turn


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
        if snapshot.iteration > self.config.max_iterations:
            return TurnDecision(TurnAction.FINISH, "iteration budget exhausted")
        if (
            self.config.max_turn_tokens
            and snapshot.tokens_spent >= self.config.max_turn_tokens
        ):
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

    # ---- stopping ----------------------------------------------------------

    def decide_after_step(self, snapshot: TurnSnapshot) -> TurnDecision:
        """The model stopped asking for tools: finish, or gate on open items?"""
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

    @staticmethod
    def nudge_message(open_items: Sequence[TodoItem]) -> str:
        lines = [TASK_GATE_LEAD]
        lines.extend(
            f"- {item.id}. {item.content}" for item in open_items[:NUDGE_ITEM_LIMIT]
        )
        lines.append(TASK_GATE_TAIL)
        return "\n".join(lines)
