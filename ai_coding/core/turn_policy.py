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
NUDGE_ITEM_LIMIT = 10


@dataclass(frozen=True)
class TurnPolicyConfig:
    """The orchestration settings this unit needs, read once per loop."""

    max_iterations: int = 10
    stop_policy: str = "todo_aware"  # natural | todo_aware | strict
    max_todo_nudges: int = 2
    task_injection: str = "message"  # message | system

    @classmethod
    def from_ai_config(cls, settings: Any) -> TurnPolicyConfig:
        """Build from ``AIConfig`` (duck-typed: fakes in tests work too)."""
        return cls(
            max_iterations=max(1, int(getattr(settings, "max_tool_iterations", 10))),
            stop_policy=str(getattr(settings, "stop_policy", "todo_aware")),
            max_todo_nudges=int(getattr(settings, "max_todo_nudges", 2)),
            task_injection=str(getattr(settings, "task_injection", "message")),
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
        return TurnDecision(TurnAction.ITERATE, "within budget")

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
