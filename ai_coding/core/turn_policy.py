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
from typing import Any

from ai_coding.core import interventions
from ai_coding.core.interventions import NUDGE_ITEM_LIMIT
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.task import TaskState, TodoItem

__all__ = [
    "NUDGE_ITEM_LIMIT",
    "TASK_MESSAGE_LEAD",
    "TaskInjection",
    "TurnAction",
    "TurnDecision",
    "TurnPolicy",
    "TurnPolicyConfig",
    "TurnSnapshot",
]

TASK_MESSAGE_LEAD = "以下是你自己的任务清单状态（系统注入，不是用户发言）："

#: N1: the terminal note a research conclusion leaves.  The other exit of a
#: plan-mode turn: the conclusion is the answer, so the turn ends with it --
#: no plan, nothing armed, nothing executed.
PLAN_RESEARCH_NOTE = "[plan] 研究已收尾：结论已给出（未提交计划、未执行任何工作）。"
#: G8-1: the marker for the one block that tells the model its budget is nearly
#: gone.  Volatile (never persisted) like the memory and task blocks -- it
#: describes a counter, not a fact about the work.
BUDGET_PRESSURE_LEAD = "[budget] 本轮预算快用完了"
#: How much of a budget may remain before the warning appears.
PRESSURE_RATIO = 0.2


@dataclass(frozen=True)
class TurnPolicyConfig:
    """The orchestration settings this unit needs, read once per loop."""

    #: Iterations one round may start.  ``-1`` means "no ceiling".
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
    max_auto_continuations: int = 3
    #: B1/B2: consecutive look-only calls before the search ladder speaks.
    #: ``0`` disables the gate -- reachable from ``AIConfig`` because this gate
    #: runs a subprocess by itself when it reaches L2, and an operator must be
    #: able to turn that off without editing code.
    search_streak_threshold: int = 4
    #: I1/R-7: consecutive look-only calls that force the turn to close.  0 = off
    #: by default -- eval-08 measured it firing on a novel-only read sweep and
    #: skipping the pathology it was meant to stop (see ``AIConfig``).
    search_streak_l3: int = 0

    @classmethod
    def from_ai_config(cls, settings: Any) -> TurnPolicyConfig:
        """Build from ``AIConfig`` (duck-typed: fakes in tests work too).

        ``max_tool_iterations <= 0`` means "no cap" and becomes ``-1``, the
        sentinel ``decide_next_iteration`` already understands.  It used to
        clamp to 1, which turned an explicit "unlimited" into the strictest
        possible turn.
        """
        raw_iterations = int(getattr(settings, "max_tool_iterations", 10))
        return cls(
            max_iterations=(
                -1 if raw_iterations <= 0 else max(1, raw_iterations)
            ),
            stop_policy=str(getattr(settings, "stop_policy", "todo_aware")),
            max_todo_nudges=int(getattr(settings, "max_todo_nudges", 2)),
            task_injection=str(getattr(settings, "task_injection", "message")),
            max_turn_tokens=int(getattr(settings, "turn_token_budget", 0) or 0),
            failed_streak_threshold=int(
                getattr(settings, "failed_streak_threshold", 2) or 0
            ),
            max_strategy_nudges=int(getattr(settings, "max_strategy_nudges", 1) or 0),
            stall_iterations=int(getattr(settings, "stall_iterations", 3) or 0),
            max_auto_continuations=int(
                getattr(settings, "max_auto_continuations", 3) or 0
            ),
            search_streak_threshold=int(
                getattr(settings, "search_streak_threshold", 4) or 0
            ),
            search_streak_l3=int(getattr(settings, "search_streak_l3", 0) or 0),
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
    #: The terminal line the operator must see.  It rides the decision, not
    #: the reason: the reason says *why the turn ended* ("iteration budget
    #: exhausted"), the note says what the turn leaves behind — two different
    #: facts.
    note: str = ""


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
    #: C-2/C-3 (codex path): this turn is the codex plan-implementation turn
    #: (``session.plan_implementing``).  It has no iteration ceiling and is
    #: never nudged/continued: a stopped model ends the turn.
    plan_implementing: bool = False
    # R6b: how the tool calls have been going.  Fed in by the loop — the policy
    # never counts anything itself, so it stays pure and table-testable.
    failed_streak: int = 0  # consecutive iterations where every call failed
    neutral_streak: int = 0  # D: consecutive rounds where every call was neutral (plan_keep etc.)
    attempted_calls: int = 0  # tool calls this turn (successful or not)
    successful_calls: int = 0  # successful tool calls this turn
    strategy_nudges: int = 0  # strategy nudges already injected this turn
    # F2: Paris' Law fatigue index — accumulated error damage.
    fatigue_index: float = 0.0
    # F2: fatigue nudges already injected this turn.
    fatigue_nudges: int = 0
    #: 13 §2.1: the per-gate ledger (see ``core/interventions.py`` for the shape
    #: and rules).  A mapping because it round-trips through JSON; empty = no
    #: gate has fired yet.
    intervention_ledger: Mapping[str, Any] = field(default_factory=dict)
    #: 13 §2.1: successful ``ask_user`` calls this turn — the helped signal of
    #: the ladder's ask levels (L3 for strategy/fatigue, L2 for task).
    ask_user_calls: int = 0
    #: B1/B2: consecutive successful *retrieval* calls with no successful
    #: non-retrieval call in between.  This is the measured shape of the T2 turn
    #: (63 successful looks, never an action) and the only part of "is this turn
    #: learning anything" the harness can compute exactly -- see
    #: docs/plan-m-b.md section 2 for why the output-text version was rejected.
    #: Fed in by the books like every other counter: the policy counts nothing.
    search_streak: int = 0
    #: Successful retrieval calls this turn, for the nudge text and the event.
    search_calls: int = 0
    #: I1/R-7: retrieval calls whose evidence was already seen this turn (same
    #: finding, possibly different params), the non-empty ones among them, and
    #: how many findings were new.  Fed in by the books like every other counter.
    stale_streak: int = 0
    repeated_nonempty: int = 0
    novel_findings: int = 0
    #: 18 §10.5: the two posture axes, read off the session by the loop.  They
    #: ride the snapshot so the decision stays pure; the defaults keep every
    #: pre-posture construction (and test) byte-identical.
    mode: str = "normal"
    readonly: bool = False


@dataclass(frozen=True)
class TaskInjection:
    """Where the task list went: the system prompt, or a trailing user block."""

    instructions: str | None = None
    message: ChatMessage | None = None


class TurnPolicy:
    """Pure decision unit: budgets in, action out."""

    def __init__(self, config: TurnPolicyConfig | None = None) -> None:
        self.config = config or TurnPolicyConfig()
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
            search_streak_threshold=self.config.search_streak_threshold,
            search_streak_l3=self.config.search_streak_l3,
        )

    # ---- budget ------------------------------------------------------------

    def decide_next_iteration(
        self, snapshot: TurnSnapshot, *, budget_expired: bool = False
    ) -> TurnDecision:
        """May ``snapshot.iteration`` start?  The turn clock wins over the count."""
        if budget_expired:
            return self._finish_at("turn deadline exceeded")
        # M1 audit fix: token exhaustion must not burn a C3 continuation.  The
        # gate is computed once and guards the continuation below; the reason
        # precedence for the plain ladder (iteration first) is unchanged.
        # C-3: plan execution (``plan_implementing``) has no token budget
        # either -- it is bounded by the turn clock and its own structural
        # continuation, not by a hidden spend cap.  Plan mode keeps its
        # existing token gate.
        token_gate = (
            bool(self.config.max_turn_tokens)
            and snapshot.tokens_spent >= self.config.max_turn_tokens
            and not snapshot.plan_implementing
        )
        # C-3: a codex implementation turn has no ceiling -- Codex's turn
        # is bounded by its own structural continuation, not by a step count.
        # P0b-fix-plan-budget: a plan turn (``mode == "plan"``) is a planning
        # round and does not inherit the build turn's step cap either; it is
        # bounded by the turn clock, the context window and the token budget,
        # and it ends when the model stops.
        ceiling = (
            -1
            if snapshot.plan_implementing or snapshot.mode == "plan"
            else int(self.config.max_iterations)
        )
        if ceiling >= 0 and snapshot.iteration > ceiling:
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
            return self._finish_at("iteration budget exhausted")
        if token_gate:
            return self._finish_at("turn token budget exhausted")
        # Plan / read-only: a planning or read-only turn is not an
        # execution-drift turn.  The *search* ladder would cut it mid-work
        # (trace f1d881ee t12: "search unconverged" at round 20 in a planning
        # turn).  Strategy/fatigue still fire: repeated tool failures are worth
        # stopping for, even in a plan.  The iteration, token and wall-clock
        # ceilings bound the rest.
        allow_search = not (
            snapshot.mode == "plan"
            or snapshot.readonly
        )
        # R6b tiers 2-3 and F2 now live in the ladder (13 §2.1): one gate fires
        # per iteration, in trigger-priority order, with memory of what the last
        # injection achieved.  The death gates and C3 above stay exactly as they
        # were — they have no ladder to climb.
        gate = interventions.select(
            snapshot, phase=interventions.PRE, limits=self._limits,
            allow_search=allow_search,
        )
        if gate is not None:
            decision = self._as_turn_decision(gate)
            if decision.action is TurnAction.FINISH:
                return self._finish_at(
                    decision.reason,
                    intervention=decision.intervention,
                    level=decision.level,
                    note=decision.note,
                )
            return decision
        return TurnDecision(TurnAction.ITERATE, "within budget")

    def _finish_at(
        self,
        reason: str,
        *,
        intervention: str = "",
        level: int = 0,
        note: str = "",
    ) -> TurnDecision:
        """End the turn on a budget mouth.

        I1/R-7: the ladder's metadata rides through.  Before this, a gate that
        chose FINISH lost ``intervention``/``level``/``note`` here, so
        ``_note_intervention`` saw no intervention (no ``SearchStalled``) and
        the terminal note never reached the outcome.
        """
        return TurnDecision(
            TurnAction.FINISH,
            reason,
            intervention=intervention,
            level=level,
            note=note,
        )

    def decide_after_step(self, snapshot: TurnSnapshot) -> TurnDecision:
        """The model stopped asking for tools: finish, or run the gate ladders?"""
        # 18 §10.5: under either posture the execution gates are silent —
        # nagging a planning or read-only session to run tools is absurd.
        # Strategy/fatigue live at the PRE point and are untouched.
        if snapshot.mode == "plan" or snapshot.readonly:
            return TurnDecision(TurnAction.FINISH, "model finished")
        if snapshot.plan_implementing:
            # C-3: Codex's implementation turn is an ordinary turn.  It has no
            # output of its own to continue -- the model's stop is the end, and
            # the task ladder must not speak (no receipts on this path).
            return TurnDecision(TurnAction.FINISH, "codex plan turn: no structural continuation")
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
            # I1/R-7: a ladder FINISH's terminal line must survive the mapping.
            note=gate.note,
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

    def budget_pressure(
        self,
        *,
        iteration: int,
        tokens_spent: int,
        mode: str = "normal",
        plan_implementing: bool = False,
    ) -> str | None:
        """A note to the model when the turn's budget is nearly gone (G8-1).

        Six separate budgets govern a turn and the model can see none of them --
        it discovers the cap by being cut off mid-plan, which is the one moment
        the work is least resumable.  This is the harness saying so while there
        is still a round left to spend on wrapping up.

        Pure and side-effect free: it reads the config it was built with and two
        numbers the caller already has.  No tokens of its own, no extra call.

        Returns ``None`` when there is nothing true to say -- a healthy budget,
        a turn that is already past its cap (the decision ladder owns that), an
        unlimited token budget, where warning about a ceiling that does not
        exist would be a lie the model acts on, or a plan turn / plan execution
        (``mode == "plan"`` or ``plan_implementing``), which have no step or
        token budget at all.  A planning turn's configured token budget still
        stops it; plan execution has neither ceiling, so any note here would
        describe a cap that does not exist.
        """
        # P0b-fix-plan-budget: a plan turn has no step ceiling at all, so the
        # step-share of this block would describe a cap that does not exist.
        # Its real bounds are the turn clock and context compression.
        # C-3: the codex plan *execution* turn (``plan_implementing``) has no
        # step or token budget either; warning it would make the model stop
        # early on a global max_iterations it does not actually inherit.
        if plan_implementing or mode == "plan":
            return None
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
                # R20/RC-5: without this exit, "don't start anything new" plus a
                # task that demands one deep answer makes the cheapest satisfying
                # move "fill the gap from memory" -- measured on T2, which
                # answered with a constructor it had never opened.
                "证据不足的部分直接写「未确认」并说明缺什么，不要补全。",
            ]
        )

    @staticmethod
    def continuation_message(open_items: Sequence[TodoItem]) -> str:
        """C3's continuation prompt: the budget died, the work did not.

        Names what is still open so the next round knows exactly what to pick
        up, and says not to redo finished work.
        """
        lines = [
            "[continue] 上一轮到了迭代上限，工作还没做完："
        ]
        lines.extend(
            f"- {item.id}. {item.content}" for item in open_items[:NUDGE_ITEM_LIMIT]
        )
        lines.append("继续把剩下的工作做完，不要重复已完成的部分。")
        return "\n".join(lines)
