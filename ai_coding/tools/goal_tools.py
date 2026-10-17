"""``start_goal`` / ``update_goal`` — the model's own taskification surface (D-4).

A goal is the lighter sibling of ``/plan``: no planning gate, no per-step proof
commands — the model claims an objective big enough to keep working across
rounds, the kernel drives one ordinary turn per round on the main session, and
the model itself declares the end (``update_goal``).  Both reference harnesses
converged on the same trust shape, and so does this pair:

* starting is only allowed from an ordinary turn — never readonly (planning),
  never while a task is already armed.  The armed check also covers the case
  that matters for autonomy: a goal round cannot start another goal, so an
  automatic run can never extend its own authority (dsh disarms on load for
  the same reason: "never inherits hidden automatic authority").
* ``complete`` is a claim accepted at any round; ``blocked`` is refused before
  the third round of the episode (dsh ``blockedAfterConsecutiveRounds``=3,
  Codex's "at least three consecutive goal turns") — one bad round is not an
  impasse.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["StartGoalTool", "UpdateGoalTool"]

#: dsh's `blockedAfterConsecutiveRounds` (packages/goal/tool-goal/src/index.ts:39)
#: and Codex's continuation.md agree on 3; kept as one named number.
BLOCKED_MIN_ROUNDS = 3

STARTED = (
    "已立案：目标为「{objective}」。本回合结束后系统会自行开第一轮，"
    "每轮结束后自动续；用 todo_write 维护步骤清单（可选）。"
    "完成时调 update_goal(complete)，同一阻塞连续 ≥3 轮才可 update_goal(blocked)。"
    "请简短收尾，本回合不要开始执行。"
)
ALREADY_ARMED = (
    "error: 已有一个在跑的任务（profile={profile}），不能再立案/重复立案——"
    "它在每轮开始时都会复查；停下它用 Esc（或 /goal off），然后重新调用。"
)
READONLY = (
    "error: 只读/规划阶段不能立案——先退出计划模式（/plan off），"
    "或等计划执行结束后再调用 start_goal。"
)
NO_GOAL = (
    "error: 当前没有在运行的 goal 任务（未 armed 或 profile 不是 goal）。"
    "update_goal 只用于申报正在跑的目标；先 start_goal 立案。"
)


class StartGoalTool(BaseTool):
    """Arm the session's task as a self-driving goal (profile ``goal``)."""

    parallel_safe = False
    mutates_world = False

    @property
    def name(self) -> str:
        return "start_goal"

    @property
    def description(self) -> str:
        return (
            "Turn the current work into a self-driving goal: the system keeps "
            "opening a new round toward the objective until you call "
            "update_goal(complete) or a hard cap/disarm stops it. Use it when "
            "the request is clearly bigger than one turn and you can name the "
            "finish line; keep the objective intact across rounds and never "
            "shrink it to what fits one round. Optional: maintain a step list "
            "with todo_write. Ends with the turn; execution starts right after "
            "it. Not available in plan mode or while a task is already armed."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": (
                        "The finish line, one sentence: what must be true for "
                        "this goal to be complete."
                    ),
                },
            },
            "required": ["objective"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        context = resolve_ctx(ctx)
        task = context.task
        objective = str(params.get("objective", "") or "").strip()
        if not objective:
            return ToolResult(
                success=False,
                output="error: 'objective' is required",
                error="missing_objective",
            )
        if context.readonly:
            return ToolResult(success=False, output=READONLY, error="readonly_session")
        if task.armed:
            return ToolResult(
                success=False,
                output=ALREADY_ARMED.format(profile=task.armed_profile or "?"),
                error="already_armed",
                data={"armedProfile": task.armed_profile},
            )
        task.arm_goal(objective)
        return ToolResult(
            success=True,
            output=STARTED.format(objective=objective),
            data={"goal": objective, "armed": True, "profile": "goal"},
        )


class UpdateGoalTool(BaseTool):
    """The model's goal-state claim: complete, or blocked past the threshold."""

    parallel_safe = False
    mutates_world = False

    @property
    def name(self) -> str:
        return "update_goal"

    @property
    def description(self) -> str:
        return (
            "Declare the state of the running goal. status=complete: every "
            "requirement is satisfied and proven by evidence you actually "
            "gathered this episode -- never for near-done, budget-exhausted or "
            "best-effort; the driver stops after this round. status=blocked: "
            "the same obstruction persisted for at least "
            f"{BLOCKED_MIN_ROUNDS} consecutive rounds and progress is impossible "
            "without the user or an external change; a note is required. "
            "Available only while a goal is running."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["complete", "blocked"],
                    "description": "The claim to record.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Required for blocked: the obstruction, in one or two "
                        "sentences."
                    ),
                },
            },
            "required": ["status"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        context = resolve_ctx(ctx)
        task = context.task
        status = str(params.get("status", "") or "").strip().lower()
        if status not in ("complete", "blocked"):
            return ToolResult(
                success=False,
                output=f"error: unknown status '{status}' (valid: complete, blocked)",
                error="invalid_status",
            )
        if not (task.armed and task.armed_profile == "goal"):
            return ToolResult(success=False, output=NO_GOAL, error="no_goal")
        note = str(params.get("note", "") or "").strip()
        if status == "blocked":
            if not note:
                return ToolResult(
                    success=False,
                    output="error: 'note' is required with status=blocked",
                    error="missing_note",
                )
            if task.rounds_run < BLOCKED_MIN_ROUNDS:
                return ToolResult(
                    success=False,
                    output=(
                        f"error: blocked 需同一阻塞连续 ≥{BLOCKED_MIN_ROUNDS} 轮才可申报"
                        f"（当前第 {task.rounds_run} 轮）。继续尝试其他手段，"
                        "或先记录观察并结束本轮。"
                    ),
                    error="block_threshold",
                    data={"roundsRun": task.rounds_run},
                )
        task.goal_phase = status
        if status == "complete":
            output = "已记录 complete：本轮做收尾说明即可，系统将在本轮结束后停下。"
        else:
            output = "已记录 blocked：本轮做收尾说明即可，系统将在本轮结束后停下。"
        return ToolResult(
            success=True,
            output=output,
            data={"status": status, "roundsRun": task.rounds_run},
        )
