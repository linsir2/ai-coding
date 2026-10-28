"""Intervention ladders — the four gates as one pure state machine (13 §2.1).

Four gates used to be one-shot nudges scattered across ``TurnPolicy``: strategy
and fatigue fired once and then nothing, the task/acceptance gates nagged until
their budget ran out and stopped.  Nothing remembered what the previous nudge
achieved, so a stuck turn either repeated the same text or gave up silently.

This module is the memory: a per-gate ledger (level / window / outcome / ref),
a pure ``select`` that answers "which gate fires what, this decision point",
``settle`` that advances observation windows, ``record`` that writes the
decision back, and ``armed`` that exposes the L2 rejection window.  No IO, no
clock, no engine — the policy calls it, the loop persists what it returns.

Ledger entry (JSON-safe, persisted under ``interventionState``):
``{gate: {level, at: [fired_at, window_closes_at], outcome, fires, ref}}``
outcome: pending | helped | failed | unfinished | exhausted (三态结算, 13 HIGH-1)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # typing only — runtime reads attributes (no import cycle)
    from ai_coding.core.turn_policy import TurnSnapshot

__all__ = [
    "ACCEPTANCE",
    "ACCEPTANCE_GATE_LEAD",
    "FATIGUE",
    "FATIGUE_GATE_LEAD",
    "FATIGUE_THRESHOLD",
    "InterventionDecision",
    "L1",
    "L2",
    "L3",
    "L4",
    "LadderLimits",
    "NUDGE_ITEM_LIMIT",
    "POST",
    "PRE",
    "SEARCH",
    "SEARCH_GATE_LEAD",
    "SEARCH_UNCONVERGED",
    "STRATEGY",
    "STRATEGY_GATE_LEAD",
    "TASK",
    "TASK_GATE_LEAD",
    "TASK_GATE_TAIL",
    "WINDOW",
    "armed",
    "record",
    "select",
    "settle",
]

STRATEGY = "strategy"
FATIGUE = "fatigue"
TASK = "task"
ACCEPTANCE = "acceptance"
#: B1/B2: the look-only ladder -- tools keep succeeding and the turn keeps not
#: acting.  The one shape the other four gates cannot see (they read failures,
#: fatigue, open items and contracts; this reads "succeeded, and still nothing").
SEARCH = "search"

PRE = "pre"    # decide_next_iteration
POST = "post"  # decide_after_step

L1, L2, L3, L4 = 1, 2, 3, 4

#: Iterations a fired window stays open before a signal-less window fails.
WINDOW = 2

#: F2: Paris' Law fatigue threshold — accumulated error damage that triggers it.
FATIGUE_THRESHOLD = 30.0

#: How many items a gate message lists.
NUDGE_ITEM_LIMIT = 10

TASK_GATE_LEAD = "[task-gate] 你还有未完成的任务项："
TASK_GATE_TAIL = "请继续推进，或把不再需要的条目用 todo_write 标记为 dropped 并说明原因。"
STRATEGY_GATE_LEAD = "[strategy-gate]"
FATIGUE_GATE_LEAD = "[fatigue-gate]"
ACCEPTANCE_GATE_LEAD = "[acceptance-gate]"
SEARCH_GATE_LEAD = "[search-gate]"
#: I1/R-7: the reason a look-only turn was forced to close.  The loop matches
#: on it (to ask for a closing word) and the digest maps it to a label.
SEARCH_UNCONVERGED = "search unconverged"
#: The operator-facing terminal line when the hard stop fires.
SEARCH_UNCONVERGED_NOTE = (
    "[search-gate] 检索未收敛：这一回合只有只读调用、没有带来新证据，已强制收尾。"
)


@dataclass(frozen=True)
class LadderLimits:
    """The triggers and L1 budgets, built once from ``TurnPolicyConfig``."""

    fail_streak_threshold: int = 2
    stall_iterations: int = 3
    strategy_l1: int = 1
    fatigue_l1: int = 1
    task_l1: int = 2
    acceptance_l1: int = 2
    #: B1/B2: consecutive look-only calls before the search ladder speaks.  4 is
    #: the smallest number a normal turn does not reach -- read, read, edit, test:
    #: the streak is back to 0 by the third call.  0 disables the gate.
    search_streak_threshold: int = 4
    #: B1/B2: the streak that earns L2 (the static check).  Two past L1 rather than
    #: twice L1 on purpose: the default turn budget is 10 iterations and a nudge
    #: costs one of them, so a rung at 8 could land on the last iteration and the
    #: model would never get to read the output the harness produced.
    search_streak_l2: int = 6
    #: I1/R-7: the streak that forces the turn to close.  0 = off, and that is
    #: the default on purpose: every pre-existing search test constructs a bare
    #: ``LadderLimits()`` and pins "the ladder never finishes the turn".  The
    #: loop opts in through ``AIConfig.search_streak_l3`` (default 0 = off:
    #: eval-08 measured it cutting a novel-only read sweep, see ``AIConfig``).
    search_streak_l3: int = 0
    #: I1/R-7: *stale* evidence (the same finding seen again) earns the rungs
    #: earlier than the streak can.  Measured: 63 looks with near-identical
    #: results and no conclusion.  0 disables a rung.
    stale_l1: int = 3
    stale_l2: int = 4
    #: The forced close: repeated evidence alone is not enough (an empty result
    #: repeated six times is a survey of empty places, not a stall), so L3 also
    #: demands ``repeated_nonempty`` -- non-empty repeated findings.
    stale_l3: int = 6


@dataclass(frozen=True)
class InterventionDecision:
    """One gate's verdict for this decision point."""

    gate: str
    level: int
    kind: str          # "nudge" | "finish"
    reason: str
    text: str = ""     # nudges only
    #: I1/R-7: the terminal line that must reach the operator when a ladder
    #: *finishes* the turn.  It rides the decision like the plan guard's note.
    note: str = ""


# ---- triggers --------------------------------------------------------------


def _strategy_triggered(snap: TurnSnapshot, limits: LadderLimits) -> bool:
    if limits.fail_streak_threshold and snap.failed_streak >= limits.fail_streak_threshold:
        return True
    return bool(
        limits.stall_iterations
        and snap.iteration > limits.stall_iterations
        and snap.attempted_calls
        and snap.successful_calls == 0
        and snap.neutral_streak == 0
    )


def _triggered(gate: str, snap: TurnSnapshot, limits: LadderLimits) -> bool:
    if gate == STRATEGY:
        return _strategy_triggered(snap, limits)
    if gate == SEARCH:
        # A planning or read-only posture is *supposed* to be look-only: nagging it
        # to "stop looking" would be wrong every single time, so the gate is off
        # there.  Measured: plan/readonly turns reached L1 and L2 within 8 reads.
        if snap.mode == "plan" or snap.readonly:
            return False
        if not limits.search_streak_threshold:
            return False
        if snap.search_streak >= limits.search_streak_threshold:
            return True
        # I1/R-7: repeated evidence earns the rungs earlier than the streak can:
        # a turn that keeps re-finding the same thing is not learning.
        return bool(limits.stale_l1 and snap.stale_streak >= limits.stale_l1)
    if gate == FATIGUE:
        return snap.fatigue_index >= FATIGUE_THRESHOLD
    if gate == TASK:
        return bool(snap.open_items)
    return any(item.passed is not True for item in snap.acceptance)


# ---- helped signals --------------------------------------------------------


def _items_sig(snap: TurnSnapshot) -> list[list[str]]:
    return [[item.id, item.status] for item in snap.open_items]


def _acc_sig(snap: TurnSnapshot) -> list[list[Any]]:
    return [[item.id, item.passed] for item in snap.acceptance]


def _asks(ref: Mapping[str, Any]) -> int:
    return int(ref.get("asks", 0) or 0)


def _helped(gate: str, entry: Mapping[str, Any], snap: TurnSnapshot) -> bool:
    """Did the signal the last injection targeted actually change?"""
    ref: Mapping[str, Any] = entry.get("ref") or {}
    level = int(entry.get("level", 0) or 0)
    if gate == STRATEGY:
        if level <= L2:
            return snap.failed_streak == 0
        return snap.ask_user_calls > _asks(ref)
    if gate == SEARCH:
        # No ``_helped`` branch: the rung is derived from the streak itself (see
        # ``_search_due``), so "did the last nudge help" has no decision to make.
        # The ledger still records an outcome for audit, and nothing reads it.
        return False
    if gate == FATIGUE:
        if level == L1:
            return snap.fatigue_index <= float(ref.get("fatigue", 0.0) or 0.0)
        if level == L2:
            return snap.failed_streak == 0
        return snap.ask_user_calls > _asks(ref)
    if gate == TASK:
        if level == L1:
            return _items_sig(snap) != [list(x) for x in ref.get("items", [])]
        return snap.ask_user_calls > _asks(ref)
    if level == L1:  # acceptance
        return _acc_sig(snap) != [list(x) for x in ref.get("acc", [])]
    return snap.ask_user_calls > _asks(ref)


def _window_closed(entry: Mapping[str, Any], snap: TurnSnapshot) -> bool:
    at = entry.get("at") or [0, 0]
    return snap.iteration >= int(at[1])


def _ref_for(gate: str, snap: TurnSnapshot) -> dict[str, Any]:
    """The baseline the next window observes against (what "helped" compares)."""
    if gate == STRATEGY:
        return {"asks": int(snap.ask_user_calls)}
    if gate == SEARCH:
        return {
            "asks": int(snap.ask_user_calls),
            "streak": int(snap.search_streak),
            "calls": int(snap.search_calls),
            # I1/R-7: the stale stream re-fires on its own threshold.
            "stale": int(snap.stale_streak),
            "novel": int(snap.novel_findings),
            "repeated": int(snap.repeated_nonempty),
        }
    if gate == FATIGUE:
        return {"asks": int(snap.ask_user_calls), "fatigue": float(snap.fatigue_index)}
    if gate == TASK:
        return {"asks": int(snap.ask_user_calls), "items": _items_sig(snap)}
    return {"asks": int(snap.ask_user_calls), "acc": _acc_sig(snap)}


# ---- the three entry points -------------------------------------------------


def settle(
    ledger: Mapping[str, Any], snapshot: TurnSnapshot, *, turn_end: bool = False
) -> dict[str, Any]:
    """Advance every open window: helped / failed (or unfinished at turn end).

    ``turn_end`` is the three-state rule (13 HIGH-1): a window the turn boundary
    cut short was never observed, so it becomes ``unfinished`` — the next turn
    retries the *same* level instead of inheriting a "failed" it never earned.
    """
    if not ledger:
        return {}
    out: dict[str, Any] = dict(ledger)
    for gate, entry in ledger.items():
        if entry.get("outcome") != "pending":
            continue
        if turn_end:
            out[gate] = {**entry, "outcome": "unfinished"}
        elif _helped(gate, entry, snapshot):
            out[gate] = {**entry, "outcome": "helped"}
        elif _window_closed(entry, snapshot):
            out[gate] = {**entry, "outcome": "failed"}
    return out


def _search_due(
    snap: TurnSnapshot, entry: Mapping[str, Any] | None, limits: LadderLimits
) -> tuple[int, str] | None:
    """The rung the look-only streak has earned, or None to stay quiet (B1/B2).

    The rung is derived from the **streak**, not from the ledger's window, which is
    why this gate does not share ``_due``'s escalation ladder.  Reason, measured end
    to end (docs/plan-m-b.md §十五): a nudge is injected *before* the model runs, and
    ``search_streak`` only decays on a successful action -- so a gate that re-armed
    from window bookkeeping alone re-fired on every iteration, and the model never
    got to run again.

    L1 at ``search_streak_threshold`` looks, L2 at ``search_streak_l2``, and either
    rung re-fires only once the streak has grown a whole L1-threshold past the last
    fire -- otherwise every extra look would inject another nudge and the model
    would spend its iterations reading advice instead of working.

    I1/R-7 adds two things on top (the streak itself is unchanged):

    * **L3 is a hard stop.** At ``search_streak_l3`` looks (0 = off, and the
      default) the turn is finished with ``search unconverged``.  The count is
      off by default because eval-08 measured what it selects for: a design
      question's 18 reads were *all* novel (0 stale) and the stop truncated an
      answer that had been complete, while the 61-call survey it targets
      interleaves ``bash`` and never builds a streak at all.
    * **Stale evidence earns rungs earlier.** ``stale_l1``/``stale_l2`` fire the
      advice rungs; ``stale_l3`` *finishes*, but only when at least two of the
      repeats carried content -- six empty results are a survey of empty places,
      not a stall.
    """
    l3_at = limits.search_streak_l3
    if l3_at and snap.search_streak >= l3_at:
        return (L3, "finish")
    if (
        limits.stale_l3
        and snap.stale_streak >= limits.stale_l3
        and snap.repeated_nonempty >= 2
    ):
        return (L3, "finish")
    threshold = limits.search_streak_threshold
    if not threshold:
        return None
    # ``+1`` rather than ``max(l2, threshold)``: a threshold at or above the L2
    # constant would otherwise earn L2 on the very same look that earned L1, and
    # L1 would never be seen.
    l2_at = max(limits.search_streak_l2, threshold + 1)
    level = 0
    if snap.search_streak >= l2_at:
        level = L2
    elif snap.search_streak >= threshold:
        level = L1
    if limits.stale_l2 and snap.stale_streak >= limits.stale_l2:
        level = max(level, L2)
    elif limits.stale_l1 and snap.stale_streak >= limits.stale_l1:
        level = max(level, L1)
    if not level:
        return None
    if entry is None:
        return (level, "nudge")
    if level > int(entry.get("level", 0) or 0):
        return (level, "nudge")
    ref: Mapping[str, Any] = entry.get("ref") or {}
    if snap.search_streak >= int(ref.get("streak", 0) or 0) + threshold:
        return (level, "nudge")
    # The stale stream grows independently of the streak (different params, same
    # evidence), so it earns its re-fire on its own whole L1-threshold.
    stale_l1 = limits.stale_l1 or threshold
    if snap.stale_streak >= int(ref.get("stale", 0) or 0) + stale_l1:
        return (level, "nudge")
    return None


def _due(
    gate: str,
    entry: Mapping[str, Any] | None,
    snap: TurnSnapshot,
    limits: LadderLimits,
) -> tuple[int, str] | None:
    """(level, kind) this gate should fire now, or None to keep observing."""
    if gate == SEARCH:
        return _search_due(snap, entry, limits)
    if entry is None:
        return (L1, "nudge")
    outcome = str(entry.get("outcome", ""))
    level = int(entry.get("level", 0) or 0)
    if outcome == "pending":
        if gate == TASK or gate == ACCEPTANCE:
            budget = limits.task_l1 if gate == TASK else limits.acceptance_l1
            if level == L1:
                fires = int(entry.get("fires", 0) or 0)
                return (L1, "nudge") if fires < budget else (L2, "nudge")
            return (L2, "finish")            # the ask was ignored
        return None                           # strategy/fatigue: observing
    if outcome == "failed":
        if gate == STRATEGY:
            return (L4, "finish") if level >= L3 else (level + 1, "nudge")
        if gate == FATIGUE:
            return (L3, "finish") if level >= L3 else (level + 1, "nudge")
        return (L2, "finish") if level >= L2 else (L2, "nudge")
    if outcome == "unfinished":
        return (max(level, L1), "nudge")      # same-level retry
    return (L1, "nudge")                      # helped / exhausted / unknown


def select(
    snapshot: TurnSnapshot, *, phase: str, limits: LadderLimits
) -> InterventionDecision | None:
    """Pick the one gate that fires at this decision point (priority order)."""
    ledger: Mapping[str, Any] = getattr(snapshot, "intervention_ledger", None) or {}
    if phase == PRE:
        # B1/B2: SEARCH last, so a turn whose calls are also *failing* hears the
        # strategy ladder first ("stop repeating the failing call").  SEARCH is a
        # PRE-only gate on purpose: at the post-step seam the model has already
        # stopped looking by itself, so there is no rung left to climb.
        gates: tuple[str, ...] = (STRATEGY, FATIGUE, SEARCH)
    else:
        # 13 §2.1（Q1 拍板）: the acceptance branch is exclusive when contracts
        # exist — the task gate never competes with it (plan-runner parity).
        gates = (ACCEPTANCE,) if snapshot.acceptance else (TASK,)
    for gate in gates:
        if not _triggered(gate, snapshot, limits):
            continue
        due = _due(gate, ledger.get(gate), snapshot, limits)
        if due is None:
            continue
        return _build(gate, due[0], due[1], snapshot)
    return None


def record(
    ledger: Mapping[str, Any], decision: Any, snapshot: TurnSnapshot
) -> dict[str, Any]:
    """Write one fired decision back: level, window, ref, fire count.

    Same-level re-fires (task/acceptance L1) refresh the window and count up;
    a level change starts fresh.  A finish lands as ``exhausted`` so the next
    turn restarts at L1 while keeping the audit trail.
    """
    gate = str(decision.intervention)
    level = int(decision.level)
    entry = ledger.get(gate)
    fires = 1
    if (
        entry is not None
        and entry.get("outcome") == "pending"
        and int(entry.get("level", 0) or 0) == level
    ):
        fires = int(entry.get("fires", 0) or 0) + 1
    action = getattr(decision, "action", None)
    finished = getattr(action, "value", action) == "finish"
    out: dict[str, Any] = dict(ledger)
    out[gate] = {
        "level": level,
        "at": [snapshot.iteration, snapshot.iteration + WINDOW],
        "outcome": "exhausted" if finished else "pending",
        "fires": fires,
        "ref": _ref_for(gate, snapshot),
    }
    return out


def armed(ledger: Mapping[str, Any] | None) -> bool:
    """True while a strategy/fatigue L2 window is open (the guard's arming)."""
    for gate, entry in (ledger or {}).items():
        if gate not in (STRATEGY, FATIGUE):
            continue
        if entry.get("outcome") == "pending" and int(entry.get("level", 0) or 0) == L2:
            return True
    return False


# ---- texts (moved from turn_policy; L1 verbatim, L2/L3 are new) ------------


def _build(gate: str, level: int, kind: str, snap: TurnSnapshot) -> InterventionDecision:
    if kind == "finish":
        if gate == SEARCH:
            # I1/R-7: the hard stop.  Its reason is what the loop matches on to
            # ask for a closing word, and what the digest labels.
            return InterventionDecision(
                gate, level, "finish", SEARCH_UNCONVERGED,
                note=SEARCH_UNCONVERGED_NOTE,
            )
        if gate == STRATEGY:
            count = snap.failed_streak or max(1, snap.iteration - 1)
            return InterventionDecision(
                gate, level, "finish",
                f"no progress: no tool call has succeeded in {count} iterations",
            )
        if gate == FATIGUE:
            return InterventionDecision(gate, level, "finish", "fatigue ladder exhausted")
        if gate == TASK:
            return InterventionDecision(gate, level, "finish", "todo ladder exhausted")
        return InterventionDecision(gate, level, "finish", "acceptance ladder exhausted")
    if gate == STRATEGY:
        reason = "repeated tool failures" if level == L1 else f"repeated tool failures (L{level})"
        return InterventionDecision(gate, level, "nudge", reason, _strategy_text(level, snap))
    if gate == SEARCH:
        reason = (
            "look-only streak"
            if level == L1
            else f"look-only streak (L{level})"
        )
        return InterventionDecision(gate, level, "nudge", reason, _search_text(level, snap))
    if gate == FATIGUE:
        reason = (
            "accumulated errors (fatigue)" if level == L1
            else f"accumulated errors (fatigue, L{level})"
        )
        return InterventionDecision(gate, level, "nudge", reason, _fatigue_text(level, snap))
    if gate == TASK:
        reason = "open todo items remain" if level == L1 else "todo items stalled: asking the user"
        return InterventionDecision(gate, level, "nudge", reason, _task_text(level, snap))
    reason = "acceptance checks failing" if level == L1 else "acceptance stalled: asking the user"
    return InterventionDecision(gate, level, "nudge", reason, _acceptance_text(level, snap))


def _strategy_text(level: int, snap: TurnSnapshot) -> str:
    if level == L1:
        count = snap.failed_streak or max(1, snap.iteration - 1)
        return "\n".join([
            f"{STRATEGY_GATE_LEAD} 最近 {count} 轮的工具调用全部失败。不要重复同样的做法：",
            "1) 先读错误输出，说清直接原因，不要猜；",
            "2) 换手段或把目标切小（换工具、换路径、先写最小复现）；",
            "3) 若确实缺信息或需要人拍板，用 ask_user 提问；没有人回答时就给出结论与假设后收尾。",
        ])
    if level == L2:
        return "\n".join([
            f"{STRATEGY_GATE_LEAD} 仍无进展：相同参数的失败调用从现在起会被直接拦截。",
            "1) 读错误输出，换一种方式；",
            "2) 或立刻用 ask_user 向用户提问，说明卡点与已经试过的方法。",
        ])
    return "\n".join([
        f"{STRATEGY_GATE_LEAD} 连续失败且换手段无效。现在必须用 ask_user 提问：",
        "说明你卡在哪里、已经试过什么、需要用户提供什么。",
    ])


def _search_text(level: int, snap: TurnSnapshot) -> str:
    """The two rungs of the search ladder (B1 + B2).

    Both rungs must read correctly for *both* shapes the streak cannot tell
    apart -- a legitimate survey and a model guessing the same wrong symbol --
    because a nudge that only fits one of them is noise half the time.
    """
    streak = max(1, snap.search_streak)
    calls = max(streak, snap.search_calls)
    if level <= L1:
        return "\n".join([
            f"{SEARCH_GATE_LEAD} 你已经连续 {streak} 次只读检索（本回合共 {calls} 次），"
            "其间没有做任何修改或验证。继续读下去，收益会越来越小：",
            "1) 如果你在找**某个符号/字符串**：换成符号名（函数名、类名、变量名），"
            "不要用自然语言描述；把范围缩小到具体目录，而不是整仓库；",
            "2) 如果你在**了解一个模块**：读它的入口文件（`main` / `__init__` / 路由注册处），"
            "不要按关键词撒网；",
            "3) 如果你已经知道要改哪里：直接改，然后用测试或命令验证，不要再搜。",
        ])
    return "\n".join([
        f"{SEARCH_GATE_LEAD} 仍是只读检索（连续 {streak} 次，共 {calls} 次），没有推进。"
        "**不要再猜关键词了** —— 让静态检查替你回答：",
        "`ruff check .`（未定义名字 F821、未用导入 F401、真 bug 都在这里）、"
        "`mypy <包名>`（模块不存在、类型不符）、"
        "`grep -rn '<符号>' --include='*.py' .`（谁在用它）。",
        "静态检查一次调用就能给出结论，比再读十个文件准。",
    ])


def _fatigue_text(level: int, snap: TurnSnapshot) -> str:
    if level == L1:
        return "\n".join([
            f"{FATIGUE_GATE_LEAD} 错误累积过高"
            f"（fatigue index: {snap.fatigue_index:.1f}）。不要继续当前路径：",
            "1) 停下来，总结你当前的理解和已取得的进展；",
            "2) 换一种完全不同的方法；",
            "3) 如果确实需要更多信息，用 ask_user 提问。",
        ])
    if level == L2:
        return "\n".join([
            f"{FATIGUE_GATE_LEAD} 错误仍在累积：相同参数的失败调用会被直接拦截。",
            "停止当前路径，换一种完全不同的方法，或立刻用 ask_user 提问。",
        ])
    return "\n".join([
        f"{FATIGUE_GATE_LEAD} 错误持续累积。现在必须用 ask_user 提问：",
        "说明卡点、已经试过的方法、需要用户提供什么。",
    ])


def _task_text(level: int, snap: TurnSnapshot) -> str:
    lines = [TASK_GATE_LEAD]
    lines.extend(
        f"- {item.id}. {item.content}" for item in snap.open_items[:NUDGE_ITEM_LIMIT]
    )
    if level == L1:
        lines.append(TASK_GATE_TAIL)
    else:
        lines.append(
            "任务项长时间没有推进。请立刻用 ask_user 向用户提问："
            "继续哪些项、收尾哪些项、还是就此停下。"
        )
    return "\n".join(lines)


def _acceptance_text(level: int, snap: TurnSnapshot) -> str:
    if level == L1:
        lines = [f"{ACCEPTANCE_GATE_LEAD} 以下验收项未通过："]
        for state in snap.acceptance[:NUDGE_ITEM_LIMIT]:
            mark = "未检查" if state.passed is None else "未通过"
            lines.append(f"- {state.id}（{mark}）")
            if state.output:
                lines.append(f"  输出：{state.output}")
        lines.append("请继续推进；修正后验收会自动重跑，无需手动重验。")
        return "\n".join(lines)
    lines = [f"{ACCEPTANCE_GATE_LEAD} 验收仍未通过："]
    lines.extend(f"- {state.id}" for state in snap.acceptance[:NUDGE_ITEM_LIMIT])
    lines.append("请立刻用 ask_user 向用户提问：如何处置未通过的验收项。")
    return "\n".join(lines)
