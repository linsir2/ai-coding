"""``present_plan`` — the model's plan, submitted for a human review (19 §3.2).

The review blocks *inside the tool*, exactly like ``ask_user``: the answer is
the tool result, so the assistant message and its ``tool`` result stay paired
(the same reason ``user_prompt.py`` exists at all).  The card on the other
side is chosen by ``intent="plan_review"`` — an intent changes presentation,
never the protocol (dsh's lesson).  Answers are a JSON string
``{"choice": "approve" | "keep", "feedback": str}``; plain words like
"开始执行" stay acceptable so the CLI path works without the card.
"""

from __future__ import annotations

import json
from typing import Any

from ai_coding.core.plan_runner import outside_workspace_message, outside_workspace_paths
from ai_coding.domain.message import ToolResult
from ai_coding.security.user_prompt import UserPromptCallback
from ai_coding.tools.base import BaseTool, ToolContext

__all__ = ["PresentPlanTool"]

#: A plan is a handful of deliverables, not one entry per file or document.
#: The measured 8-item submission (2026-10-31) is refused here rather than
#: becoming eight runner checkpoints.
MAX_PLAN_ITEMS = 6

# Room above the wait for the review to be rendered and answered, matching
# ask_user (the dispatcher takes min(this, what is left of the turn)).
_TIMEOUT_GRACE = 15.0

_APPROVED = (
    "计划已批准：执行由系统接管（前端将自动开始执行）。请简短收尾，不要开始执行。"
)
_KEEP = "用户选择继续规划。反馈：{feedback}。修改计划后再次调用 present_plan。"
#: C3: keep with no feedback.  The card's button means "there is a problem" --
#: but the problem is the human's to state, and the original design says so
#: ("若无反馈则：请询问要改什么").  Without this the model resubmitted the same
#: plan until the planning budget died (measured: 25 cards, MAX_ITERATIONS).
_ASK_FOR_CHANGES = (
    "用户选择继续规划，但没有说明要改什么。请先调用 ask_user 向用户提问："
    "「这份计划哪里需要调整？」。选项要你自己根据这份计划判断，"
    "给 2-4 个具体、互不相同的调整方向，不要套用固定模板；"
    "用户也可以自己填写。拿到回答后再按它修订并重新提交计划。"
    "如果用户没有回答，直接用一句话收尾，不要再次提交同一份计划。"
)
_NEEDS_QUESTION_FIRST = (
    "error: 上一轮评审要求继续规划，还没有问用户要改什么。"
    "先用 ask_user 提问（给出 2-4 个具体选项），拿到回答后再提交修订版。"
)
_HOLD = (
    "用户搁置了这次评审：本轮到此结束，不会执行任何计划。"
    "保持计划模式，等待用户的下一步指示。"
)
_NO_CHANNEL = (
    "当前没有人工可评审这份计划。保持计划模式，简要收尾并说明计划，等待用户指示。"
)
_DISMISSED = (
    "本次评审已被搁置（人选择了「稍后」）：本回合不再提交计划。"
    "请用一句话说明你的结论，或等待用户的下一步指示。"
)
_NOT_IN_PLAN = (
    "present_plan 只在计划模式可用；当前不在计划模式。"
    "请直接以回答收尾，不要提交计划；需要制定计划时，让用户先进入计划模式（/plan）。"
)


class PresentPlanTool(BaseTool):
    """Submit the plan for review; the answer decides approve / keep planning."""

    parallel_safe = False
    mutates_world = False

    def __init__(self, prompt: UserPromptCallback, timeout_seconds: float = 300.0) -> None:
        self._prompt = prompt
        self.timeout_seconds = float(timeout_seconds)

    @property
    def name(self) -> str:
        return "present_plan"

    @property
    def max_duration_seconds(self) -> float | None:
        """Per-call ceiling for the dispatcher (R2a)."""
        return self.timeout_seconds + _TIMEOUT_GRACE

    @property
    def description(self) -> str:
        return (
            "Present your plan for the user's review and, on approval, hand execution to "
            "the runner. Use it at the end of planning. Provide 'plan' as a concise "
            "markdown summary (a title first) for the human, and 'items' as the "
            "executable steps — one entry per step, each with its 'verify' shell "
            "command when the outcome is machine-checkable. Approval makes the system "
            "execute the items automatically — do not execute them yourself; 'keep "
            "planning' brings their feedback back, so revise and call this tool again. "
            "Only available in plan mode."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The plan as markdown: a title and a concise summary.",
                },
                "items": {
                    "type": "array",
                    "description": (
                        "The executable work items, in order; one item per "
                        "deliverable outcome (at most six for an implementation "
                        "plan), not one per file or document. Approval writes "
                        "these into the task list the runner executes; do not also "
                        "write them with todo_write."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "One step, small and actionable.",
                            },
                            "verify": {
                                "type": "string",
                                "description": (
                                    "Shell command proving user-visible behaviour; "
                                    "omit when the outcome is not machine-checkable. "
                                    "Never use a file this plan produces as its own "
                                    "proof."
                                ),
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 0},
                                "description": (
                                    "0-based indices of earlier steps this step "
                                    "needs; forward references only."
                                ),
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            "required": ["plan", "items"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        # R03: plan-only, and never over a task another profile owns --
        # approval replaces the list and arms it, so a running goal/step task
        # must be out of reach.  Both refusals fire before parsing or review;
        # a missing context fails closed the same way.
        if ctx is None or getattr(ctx, "mode", "normal") != "plan":
            return ToolResult(success=False, output=_NOT_IN_PLAN, error="not_in_plan")
        task = ctx.task
        # R03: another profile owns the task -- a running goal must not be
        # hijacked by a plan submission.
        if getattr(task, "armed", False) and task.armed_profile != "plan":
            return ToolResult(
                success=False,
                output=(
                    f"目标任务（profile={task.armed_profile or '?'}）正在运行；"
                    "等它结束或先停止它，再提交计划。"
                ),
                error="task_busy",
            )
        # B (audit fix): approval is terminal.  Once a plan is armed it owns the
        # task -- a second submission would silently replace the list a human
        # already agreed to, and if that second card were dismissed the turn
        # would end claiming nothing ran while the first plan stayed armed for
        # the client (and the next boot) to run.  /plan run disarms on its way
        # out, so a finished plan is submittable again; ``/plan off`` is the way
        # out when a run never got dispatched.
        if getattr(task, "armed", False):
            return ToolResult(
                success=False,
                output=(
                    "本次计划已批准，执行由系统接管；请简短收尾，不要开始执行。"
                    "要换一份计划，先 /plan off 退出计划模式再重新进入。"
                ),
                error="plan_already_approved",
            )
        # N1: the other exit already closed this turn (a research conclusion).
        # One turn, one exit: the plan must not be asked for after the model
        # has already given its conclusion.
        conclusion = getattr(ctx, "conclusion", None)
        done = str(getattr(conclusion, "exit", "") or "")
        if done:
            return ToolResult(
                success=False,
                output=(
                    f"本次回合已经收尾（{done}）：不能再提交计划。"
                    "请用一句话把结论说清楚。"
                ),
                error="already_concluded",
            )
        # N1: a dismissed review is not an exit -- it is "not now", so the
        # other exit stays open.  It does close the review itself: the human
        # was already asked once in this turn, and asking again would put a
        # second card in front of them (and a second approval would arm a
        # plan the dismissal then takes back).
        if getattr(conclusion, "dismissed", False):
            return ToolResult(
                success=False,
                output=_DISMISSED,
                error="already_concluded",
            )
        # C3: the human asked for a revision without saying what.  The reason
        # is theirs to give, so a revised plan can only follow an answered
        # question.  Telling the model once is enough; submitting twice ends
        # the turn like a held review instead of looping to the budget.
        review_state = str(getattr(conclusion, "plan_review_state", "") or "")
        if review_state == "refused":
            if conclusion is not None:
                conclusion.dismissed = True
            return ToolResult(
                success=True,
                output=_HOLD,
                data={"approved": False, "dismissed": True, "reviewed": True},
            )
        if review_state == "needs_feedback":
            if conclusion is not None:
                conclusion.plan_review_state = "refused"
            return ToolResult(
                success=False,
                output=_NEEDS_QUESTION_FIRST,
                error="plan_feedback_needed",
                data={"approved": False, "reviewed": True},
            )
        plan = str(params.get("plan", "") or "").strip()
        if not plan:
            return ToolResult(
                success=False,
                output="error: 'plan' is required",
                error="missing_plan",
            )
        # P1: the steps ride this call (trace ec89d5ee measured the one-add-per-
        # iteration burn).  Validated before the human is asked -- a submission
        # that cannot execute must not become a review card.
        items, invalid = _parse_items(params.get("items"))
        if invalid is None and len(items) > MAX_PLAN_ITEMS:
            # Measured 2026-10-31: an 8-item plan was approved despite the
            # "at most six" guidance.  A plan is a handful of deliverables, not
            # one line per file; refusing here is what keeps the runner from
            # turning a 6-deliverable task into 8 checkpoints.
            return ToolResult(
                success=False,
                output=(
                    f"error: 计划有 {len(items)} 个条目，超过上限 {MAX_PLAN_ITEMS}；"
                    "请合并成 ≤6 个工作项（按交付物，不要按文件/文档拆条），"
                    "然后再次调用 present_plan。"
                ),
                error="too_many_items",
            )
        if invalid is None:
            # A proof the shell cannot run is not a proof: it would fail at
            # execution, after the operator had already approved the plan.
            invalid = await self._proof_problem(items, ctx)
        if invalid is not None:
            return ToolResult(
                success=False,
                output=f"error: item {invalid[0]} {invalid[1]}",
                error="invalid_items",
            )
        if not items:
            return ToolResult(
                success=False,
                output=(
                    "计划尚未写成条目：请把全部可执行步骤放进本次调用的 items 数组"
                    "（每步一条，含 verify 命令），然后再次调用 present_plan。"
                ),
                error="no_plan_items",
            )
        # W2/R02: the workspace gate, before the human is asked.  A review card
        # for a plan the file tools cannot execute wastes the human's attention
        # and the model's turn; the model can revise the path itself, so the
        # refusal has to reach it as a tool error.  The gate is the same one
        # the runner uses, with the same roots, so the two never disagree
        # about which paths are reachable.
        workspace = str(getattr(ctx, "workspace", "") or ".")
        extra_roots = tuple(getattr(ctx, "extra_workspace_roots", ()) or ())
        # The plan body and the step descriptions are prose: the shape
        # heuristic keeps a route like ``GET /api/v1/users`` out of it.
        offenders = outside_workspace_paths(
            [plan, *(content for content, _, _ in items)],
            workspace,
            extra_roots=extra_roots,
        )
        # The proofs are shell commands, and they run *outside* the dispatch
        # gate -- refusing them here is the last point before a human approves
        # a plan the shell will run elsewhere.  Read by the shell grammar, not
        # by the prose scan: a variable or command substitution hides a path
        # from the latter (measurably, W2's live check).
        if not offenders:
            offenders = outside_workspace_paths(
                [verify for _, verify, _ in items if verify],
                workspace,
                extra_roots=extra_roots,
                from_shell=True,
            )
        if offenders:
            return ToolResult(
                success=False,
                output=outside_workspace_message(offenders),
                error="workspace_paths",
            )
        # R04: the card renders what approval will write -- the same validated
        # triples, with depends_on in the 1-based display indices the table
        # shows (approval maps them back onto ids).
        review_items = [
            {"content": content, "verify": verify, "dependsOn": [d + 1 for d in deps]}
            for content, verify, deps in items
        ]
        try:
            answer = await self._prompt.ask(
                "批准这个计划并开始执行？",
                ("开始执行", "继续规划"),
                detail=plan,
                intent="plan_review",
                data={"items": review_items},
            )
        except Exception as exc:  # noqa: BLE001 — a tool boundary must not leak
            return ToolResult(
                success=False,
                output=f"error: could not ask the user: {exc}. {_NO_CHANNEL}",
                error="user_prompt_error",
                data={"reason": str(exc)},
            )
        if str(answer.reason) == "no_user":
            return ToolResult(
                success=False,
                output=_NO_CHANNEL,
                error="no_user",
                data={"approved": False, "reason": answer.reason},
            )
        if not answer.answered:
            # Dismissed (esc / empty): information for the model, not a retryable
            # error.  Nothing can be armed here -- the gate above refuses any
            # submission while a plan runs, so a dismissal always meets an
            # unarmed task.  (The loop's dismissal branch is the one that can
            # meet an armed task; it disarms there.)
            # A dismissal is not an exit: it closes the review, not the turn
            # (N1: 占位 = 成功).  The research exit stays open, so the model
            # can still give its conclusion after the human said "not now".
            if conclusion is not None:
                conclusion.dismissed = True
            return ToolResult(
                success=True,
                output=_HOLD,
                data={"approved": False, "dismissed": True},
            )

        choice, feedback = _parse_answer(answer.text)
        if choice == "approve":
            # 11-plan T4 (Q1=A): approval IS the consent — arm the live task
            # here so the closing turn's own save carries it to disk and a
            # crash before the run dispatch still auto-resumes on boot.
            # (R03: the gates above guarantee ctx/task; the old defensive
            # None-checks are gone.)
            # P1: approval is the one write point, and it REPLACES the
            # list -- dsh's shape (``tool-todo/src/index.ts:80`` sends the
            # whole table).  Appending would let a model that pre-wrote
            # items with todo_write leave orphan steps behind, and the
            # runner executes everything open.  The goal stays: it belongs
            # to the session/episode, not to this list.
            #
            # Stale step receipts are not this tool's business: the runner
            # drops the ones whose statement is no longer on the list
            # (``plan_runner._drop_foreign_receipts``).
            ctx.task.items.clear()
            for content, verify, deps in items:
                # R09: depends_on arrives as 0-based indices into this very
                # submission; the clear above makes ``add`` assign ids 1..n,
                # so ``d + 1`` is the id the runner's blockers read.
                ctx.task.add(
                    content, verify=verify, depends_on=[str(d + 1) for d in deps]
                )
            ctx.task.armed = True
            ctx.task.armed_profile = "plan"
            # R10: approval starts a fresh episode -- the previous plan's
            # rounds, resume budget and token spend must not ride into this
            # one, and the session's ladder windows restart through the
            # loop's callback (goal_phase belongs to the goal profile and is
            # untouched).
            ctx.task.rounds_run = 0
            ctx.task.resume_count = 0
            ctx.task.tokens_used = 0
            # M-C3: the resume gate counts within an episode too.  Without
            # these two, a plan the gate just disarmed would be disarmed again
            # on the very next boot -- the fingerprint it left behind matches
            # the freshly-approved episode's opening state.
            ctx.task.no_progress_resumes = 0
            ctx.task.resume_fingerprint = None
            if ctx.note_plan_approved is not None:
                ctx.note_plan_approved()
            # N1: the plan is this turn's exit; the other one is now refused.
            if conclusion is not None:
                conclusion.exit = "present_plan"
            return ToolResult(success=True, output=_APPROVED, data={"approved": True})
        # R08 (dsh): keep is a tool error carrying the feedback -- the model
        # revises and resubmits in the same turn; an error (not a success)
        # is what stops it from counting as a submission.  ``reviewed`` tells
        # the turn guard that a plan *was* shown (the guard must not chase a
        # submission that already happened).
        if feedback:
            return ToolResult(
                success=False,
                output=_KEEP.format(feedback=feedback),
                error="plan_keep",
                data={"approved": False, "reviewed": True},
            )
        # C3: keep with no reason.  The human said "there is a problem" and
        # nothing more, so the model has to ask what it is -- HITL with
        # concrete choices.  The turn stays open for that question; the loop
        # pays the debt when ask_user gets an answer.
        if conclusion is not None:
            conclusion.plan_review_state = "needs_feedback"
        return ToolResult(
            success=False,
            output=_ASK_FOR_CHANGES,
            error="plan_keep",
            data={"approved": False, "reviewed": True, "needs_feedback": True},
        )

    async def _proof_problem(
        self, items: list[tuple[str, str, list[int]]], ctx: ToolContext
    ) -> tuple[int, str] | None:
        """The first step whose proof command cannot run, with the reason."""
        from ai_coding.core.acceptance import verify_command_problem

        for index, (_content, verify, _depends) in enumerate(items):
            if not verify.strip():
                # The schema says a step may omit its proof when the
                # outcome is not machine-checkable (that step settles as
                # stalled).  So the check applies to the proofs that are
                # there, and does not invent a requirement.
                continue
            why = await verify_command_problem(verify)
            if why:
                return index, why
        return None


def _parse_items(
    raw: Any,
) -> tuple[list[tuple[str, str, list[int]]], tuple[int, str] | None]:
    """``(items, problem)`` — ``(content, verify, depends_on)`` triples, or the
    first entry that cannot run as ``(index, why)``.

    Strict about reachability, not generous about shape: ``items`` must be a
    list of objects with a non-empty ``content`` (the old docstring claimed a
    JSON string was accepted -- it never was: a string lands in the "no items"
    refusal).  ``depends_on`` holds 0-based indices into this same submission
    and may only point backwards (``0 <= d < index``); duplicates collapse,
    first occurrence wins.  Approval maps the indices onto the ids it is about
    to assign, so an out-of-range, forward or self reference is refused here,
    before the human is asked.
    """
    if not isinstance(raw, list):
        return [], None
    parsed: list[tuple[str, str, list[int]]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return [], (index, "is not an object")
        content = str(entry.get("content", "") or "").strip()
        if not content:
            return [], (index, "has no 'content'")
        raw_deps = entry.get("depends_on", [])
        if not isinstance(raw_deps, list):
            return [], (index, "has a bad 'depends_on' (earlier-step indices only)")
        deps: list[int] = []
        for dep in raw_deps:
            if isinstance(dep, bool) or not isinstance(dep, int) or not (0 <= dep < index):
                return [], (index, "has a bad 'depends_on' (earlier-step indices only)")
            if dep not in deps:
                deps.append(dep)
        parsed.append((content, str(entry.get("verify", "") or "").strip(), deps))
    return parsed, None


def _parse_answer(text: str) -> tuple[str, str]:
    """(choice, feedback): JSON first, plain words as the fallback (CLI path).

    R08 (dsh): approval is exact -- an approve carrying custom text is a
    revision request (keep), not consent.
    """
    raw = text.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            choice = str(data.get("choice", "") or "")
            feedback = str(data.get("feedback", "") or "").strip()
            if choice in ("approve", "keep"):
                if choice == "approve" and feedback:
                    return "keep", feedback
                return choice, feedback
        except ValueError:
            pass
    if raw == "开始执行":
        return "approve", ""
    if raw == "继续规划":
        # The card's own label, typed as plain text (the CLI path): the human
        # picked the option without adding feedback -- a hold, not feedback.
        return "keep", ""
    return "keep", raw
