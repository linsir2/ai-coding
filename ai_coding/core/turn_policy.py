"""Turn policy — task injection and the stop decision, as a pure unit (R2c).

Extracted from ``core/agent_loop.py`` (its fan-out was 21, the highest in the
project, and this was the part that had to be read as a whole).  Everything here
is pure: no IO, no asyncio, no events, no persistence — so the decision rules can
be enumerated in a table test (``tests/test_turn_policy.py``).

The loop keeps the choreography (engine step, dispatch, checkpoint, events); this
unit answers two questions:

* **may another iteration start**, given the iteration budget and the turn clock?
* **how does a finished model step end**, given the task list and the nudge budget?

It also owns the volatile ``<task-state>`` block for the task list (attached to
a request only, never persisted) and the C3 continuation text; the four gate
ladders and their texts live in ``core/interventions.py`` (13 §2.1).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ai_coding.core import interventions
from ai_coding.core.interventions import NUDGE_ITEM_LIMIT
from ai_coding.domain.acceptance import AcceptanceState
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.task import TaskState, TodoItem

__all__ = [
    "NUDGE_ITEM_LIMIT",
    "TASK_MESSAGE_LEAD",
    "PlanSubmissionGuard",
    "StopGuard",
    "TaskInjection",
    "TurnAction",
    "TurnDecision",
    "TurnPolicy",
    "TurnPolicyConfig",
    "TurnSnapshot",
]

TASK_MESSAGE_LEAD = "以下是你自己的任务清单状态（系统注入，不是用户发言）："

#: stage 4 r2: the plan submission guard's two texts, beside the rules they
#: serve.  The nudge is the one remedy a plan-posture turn gets before it is
#: allowed to end without a submission (dsh's turn-stopping idea); the note
#: rides the outcome to the transcript when even the remedy is refused.
#: Measured silent-completion twice: sessions 7ad86804 and 1fd3be1e.
PLAN_NUDGE = (
    "[plan] 尚未提交计划：把全部可执行步骤放进 present_plan 的 items 数组"
    "（每步一条，含 verify 命令）提交评审，不能以纯文本收尾。请立即补交。"
)
PLAN_UNSUBMITTED_NOTE = (
    "[plan] 本回合未提交计划（未调用 present_plan）；可继续对话让它提交。"
)
#: G8-1: the marker for the one block that tells the model its budget is nearly
#: gone.  Volatile (never persisted) like the memory and task blocks -- it
#: describes a counter, not a fact about the work.
BUDGET_PRESSURE_LEAD = "[budget] 本轮预算快用完了"
#: How much of a budget may remain before the warning appears.
PRESSURE_RATIO = 0.2


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
    #: 13 §2.1: which ladder fired, and at what level — the loop records it.
    intervention: str = ""
    level: int = 0


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
    #: 13 §2.1: the per-gate ledger (see ``core/interventions.py`` for the shape
    #: and rules).  A mapping because it round-trips through JSON; empty = no
    #: gate has fired yet.
    intervention_ledger: Mapping[str, Any] = field(default_factory=dict)
    #: 13 §2.1: successful ``ask_user`` calls this turn — the helped signal of
    #: the ask levels (L3 for strategy/fatigue, L2 for task/acceptance).
    ask_user_calls: int = 0
    #: 18 §10.5: the two posture axes, read off the session by the loop.  They
    #: ride the snapshot so the decision stays pure; the defaults keep every
    #: pre-posture construction (and test) byte-identical.
    mode: str = "normal"
    readonly: bool = False
    #: stage 4 r2: the plan submission guard's view -- a plan has been
    #: submitted this turn (``present_plan`` succeeded), and the one remedy
    #: nudge has been spent.  Defaults keep earlier constructions identical.
    plan_submitted: bool = False
    plan_nudged: bool = False


@dataclass(frozen=True)
class TaskInjection:
    """Where the task list went: the system prompt, or a trailing user block."""

    instructions: str | None = None
    message: ChatMessage | None = None


class StopGuard(Protocol):
    """One rule consulted before a posture turn is allowed to end.

    dsh's ``agent/turn-stopping`` at our post-step seam: the guards run in
    order, the first verdict wins, and ``None`` passes to the next.  The
    default list holds exactly one rule — the plan submission guard below —
    because that is the only stop interceptor this harness has today; a
    second one is the trigger to add another entry, not to widen this one.
    """

    def on_stop(self, snapshot: TurnSnapshot) -> TurnDecision | None: ...


class PlanSubmissionGuard:
    """stage 4 r2: one remedy before a plan-posture turn ends unsubmitted.

    A posture turn that never submitted a plan gets one nudge; refusing it
    too ends with the terminal note instead of a silent COMPLETED (7ad86804
    / 1fd3be1e measured the silent case twice — a bare analysis, no card, no
    execution).  The two texts live beside this rule, above.
    """

    def on_stop(self, snapshot: TurnSnapshot) -> TurnDecision | None:
        if snapshot.plan_submitted:
            return None
        if not snapshot.plan_nudged:
            return TurnDecision(TurnAction.NUDGE, "plan unsubmitted", inject=PLAN_NUDGE)
        return TurnDecision(TurnAction.FINISH, "plan unsubmitted")


class TurnPolicy:
    """Pure decision unit: budgets in, action out."""

    def __init__(
        self,
        config: TurnPolicyConfig | None = None,
        *,
        stop_guards: tuple[StopGuard, ...] | None = None,
    ) -> None:
        self.config = config or TurnPolicyConfig()
        # The seam's rule list.  ``None`` means the default; an explicit tuple
        # replaces it (tests, and any future second rule), so the plan guard
        # is a voter, not the ballot box.
        self._stop_guards = (
            stop_guards if stop_guards is not None else (PlanSubmissionGuard(),)
        )
        # 13 §2.1: the ladder reads these; strict = "never give up early", so
        # its task budget is unbounded (docs/r1-analysis.md D5 keeps meaning).
        self._limits = interventions.LadderLimits(
            fail_streak_threshold=self.config.failed_streak_threshold,
            stall_iterations=self.config.stall_iterations,
            strategy_l1=self.config.max_strategy_nudges,
            fatigue_l1=1,
            task_l1=(
                10**9
                if self.config.stop_policy == "strict"
                else self.config.max_todo_nudges
            ),
            acceptance_l1=self.config.max_acceptance_nudges,
        )

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
        # R6b tiers 2-3 and F2 now live in the ladder (13 §2.1): one gate fires
        # per iteration, in trigger-priority order, with memory of what the last
        # injection achieved.  The death gates and C3 above stay exactly as they
        # were — they have no ladder to climb.
        gate = interventions.select(
            snapshot, phase=interventions.PRE, limits=self._limits
        )
        if gate is not None:
            return self._as_turn_decision(gate)
        return TurnDecision(TurnAction.ITERATE, "within budget")

    # ---- stopping ----------------------------------------------------------

    def decide_after_step(self, snapshot: TurnSnapshot) -> TurnDecision:
        """The model stopped asking for tools: finish, or run the gate ladders?"""
        # 18 §10.5: under either posture the execution gates are silent —
        # nagging a planning or read-only session to run tools is absurd.
        # Strategy/fatigue live at the PRE point and are untouched.
        if snapshot.mode == "plan" or snapshot.readonly:
            # dsh's turn-stopping at our post-step seam: the stop guards run
            # in order, the first verdict owns the decision.  Today's list is
            # the plan submission guard (its silent-COMPLETED measurements
            # 7ad86804 / 1fd3be1e are recorded beside its texts).
            for guard in self._stop_guards:
                verdict = guard.on_stop(snapshot)
                if verdict is not None:
                    return verdict
            return TurnDecision(TurnAction.FINISH, "model finished")
        if snapshot.acceptance:
            # R0-1: a turn with contracts does not end on the model's word.
            # Unchecked items fail closed (the loop verifies before asking, so
            # ``None`` here means "could not be checked" — never "passed").
            failed = [a for a in snapshot.acceptance if a.passed is not True]
            if not failed:
                return TurnDecision(TurnAction.FINISH, "acceptance verified")
            # 13 §2.1（Q1 拍板）: the acceptance branch is exclusive — the task
            # gate never competes while contracts exist (plan-runner parity).
            gate = interventions.select(
                snapshot, phase=interventions.POST, limits=self._limits
            )
            if gate is not None:
                return self._as_turn_decision(gate)
            return TurnDecision(TurnAction.FINISH, "acceptance ladder exhausted")
        if not snapshot.open_items or self.config.stop_policy == "natural":
            return TurnDecision(TurnAction.FINISH, "model finished")
        gate = interventions.select(
            snapshot, phase=interventions.POST, limits=self._limits
        )
        if gate is not None:
            return self._as_turn_decision(gate)
        return TurnDecision(TurnAction.FINISH, "model finished")

    @staticmethod
    def _as_turn_decision(gate: interventions.InterventionDecision) -> TurnDecision:
        """One ladder verdict, mapped onto the loop's decision type."""
        action = TurnAction.FINISH if gate.kind == "finish" else TurnAction.NUDGE
        return TurnDecision(
            action,
            gate.reason,
            inject=gate.text or None,
            intervention=gate.gate,
            level=gate.level,
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
        up, and says not to redo finished work.
        """
        lines = [
            "[continue] 上一轮预算耗尽，但还有未完成的任务项："
        ]
        lines.extend(
            f"- {item.id}. {item.content}" for item in open_items[:NUDGE_ITEM_LIMIT]
        )
        lines.append("继续未完成的工作，不要重复已完成的部分。")
        return "\n".join(lines)
