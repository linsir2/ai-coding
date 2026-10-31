"""Plan-as-Goal executor (stage 2): one continuous turn on the main session.

The approved plan is a durable objective.  ``/plan run`` executes it in the
**parent** session: one turn opens, the model works through the items, and when
it stops with items left the loop's ``plan`` stop policy injects the remaining
list and continues the same turn (opencode's idle hook / Claude's ``/goal``).
No per-item ``process_input`` boundary, no fresh step/wave session.

Verification stays outside the turn: the executor runs each completed item's
proof command afterwards, so the acceptance ladder can never turn an autonomous
run into ``ask_user``.  A failed proof reverts the item to ``in_progress`` and
one bounded repair turn follows; still failing means ``blocked`` and its
dependents are skipped.

Everything else is inherited from ``PlanRunner``: the workspace gate, the
arm/disarm lifecycle, receipts, delivery scoring, prompt markers and the report.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ai_coding.core.events import PlanStepReceipt
from ai_coding.core.plan_runner import (
    PlanReport,
    PlanRunner,
    _tail,
    outside_workspace_message,
)
from ai_coding.core.turn_policy import PLAN_CONTINUE, TurnPolicy
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.infra.deadline import Deadline
from ai_coding.infra.time_utils import now_utc

__all__ = [
    "NO_PROGRESS_TURNS",
    "PLAN_CONTINUE_HEAD",
    "PLAN_RUN_HEAD",
    "PlanExecutor",
]

#: The kickoff prompt's marker.  It is the harness speaking, not the human, so
#: it is sent ``synthetic=True``.
PLAN_RUN_HEAD = "[plan-run] 开始执行计划："
PLAN_CONTINUE_HEAD = "[plan-run] 继续执行："

#: A turn that made no tool call and settled nothing means the plan policy
#: already exhausted its nudges; another turn would repeat that cost.
NO_PROGRESS_TURNS = 1


class PlanExecutor(PlanRunner):
    """The ``plan`` executor that runs the whole plan in one main-session turn."""

    def __init__(self, loop: Any) -> None:
        super().__init__(loop)
        #: Completed receipts from an earlier run: never settled again.
        self._old_completed: set[str] = set()
        #: Receipts this run wrote.
        self._recorded_this_run: set[str] = set()

    # ---- lifecycle -------------------------------------------------------

    async def run(
        self,
        session: Any,
        console: Any = None,
        *,
        on_delta: Any = None,
        observer: Any = None,
    ) -> PlanReport:
        self._force_build_posture(session, observer)
        task = getattr(session, "task", None)
        report = self.new_report()
        offenders = self._workspace_offenders(task)
        if offenders:
            report.stopped = outside_workspace_message(offenders)
            if task is not None and task.disarm():
                self._persist(session)
                self._emit_task(observer, task)
            return report
        if task is None:
            report.stopped = "no-open-items"
            self._say(console, "[plan] 完成 0 步（无任务项）")
            return report

        task.armed = True
        task.armed_profile = "plan"
        self._persist(session)
        self._emit_task(observer, task)
        self._old_completed = {
            entry.step_id
            for entry in getattr(session, "step_deliveries", ()) or ()
            if entry.status == "completed"
        }
        self._recorded_this_run = set()

        settings = getattr(self._loop, "settings", None)
        plan_limit = float(
            getattr(settings, "plan_item_wall_seconds", 0.0) or 0.0
        )
        original_policy = getattr(self._loop, "policy", None)
        session.item_deadline = (
            Deadline.after(plan_limit) if plan_limit > 0 else None
        )
        session.step_item_id = ""
        session.acceptance = []
        session.plan_executing = True
        if original_policy is not None:
            self._loop.policy = TurnPolicy(
                replace(
                    original_policy.config,
                    max_iterations=max(1, self._step_budget()),
                    stop_policy=PLAN_CONTINUE,
                    max_auto_continuations=1,
                    max_plan_continues=max(
                        0, int(getattr(settings, "plan_max_continues", 30) or 0)
                    ),
                )
            )
        failures: dict[str, str] = {}
        max_turns = max(
            2,
            int(
                getattr(settings, "plan_max_item_attempts", 2)
                or 2
            ),
        )
        try:
            # A crash/approval can leave "completed but no receipt" entries;
            # settle (and verify) them before deciding whether anything is open.
            failures = await self._settle_completed(
                session, task, report, observer
            )
            turns = 0
            while True:
                pending = task.open_items()
                actionable = [
                    item for item in pending if item.status != "blocked"
                ]
                # A plan whose only leftovers are blocked is finished, not
                # budget-exhausted: the blocked verdict is the report.
                if not actionable:
                    break
                if turns >= max_turns:
                    report.stopped = (
                        f"plan turn budget exhausted: {len(actionable)} item(s) "
                        "remain open"
                    )
                    break
                prompt = (
                    self._kickoff_prompt(task, actionable)
                    if turns == 0
                    else self._continue_prompt(task, actionable, failures)
                )
                verdicts_before = self._verdict_count(report)
                tools_before = self._tool_calls(session)
                states_before = {
                    item.id: item.status for item in task.items
                }
                await self._loop.process_input(
                    session,
                    prompt,
                    on_delta=on_delta,
                    observer=observer,
                    synthetic=True,
                )
                turns += 1
                report.turns_used += 1
                usage = self._turn_usage()
                if usage:
                    report.tokens_used = self._merge_usage(report.tokens_used, usage)
                    task.plan_tokens_used = int(
                        getattr(task, "plan_tokens_used", 0) or 0
                    ) + int(usage.get("total_tokens", 0) or 0)
                failures = await self._settle_completed(
                    session, task, report, observer
                )
                budget = int(getattr(settings, "plan_token_budget", 0) or 0)
                if budget > 0 and int(getattr(task, "plan_tokens_used", 0) or 0) >= budget:
                    report.stopped = (
                        f"budget: plan token budget exhausted "
                        f"({task.plan_tokens_used}/{budget})"
                    )
                    break
                progressed = (
                    self._verdict_count(report) > verdicts_before
                    or self._tool_calls(session) > tools_before
                    or self._states_changed(task, states_before)
                )
                if not progressed:
                    report.stopped = (
                        "no progress: the turn made no tool call and settled "
                        "nothing; remaining items stay open"
                    )
                    break
                remaining = [
                    item for item in task.open_items() if item.status != "blocked"
                ]
                if not remaining:
                    break
        finally:
            if original_policy is not None:
                self._loop.policy = original_policy
            session.plan_executing = False
            session.item_deadline = None
            session.step_item_id = ""
            session.acceptance = []
            # ``acceptance`` is persisted by ``SessionData.to_dict`` -- the
            # cleared copy must reach disk as well.
            self._persist(session)
            if task.disarm():
                self._persist(session)
            self._emit_task(observer, task)

        await self._settle_open_leftovers(session, task, report, failures, observer)
        self._persist(session)
        self._emit_task(observer, task)
        self._say(console, report.render())
        return report

    # ---- prompts ---------------------------------------------------------

    def _kickoff_prompt(self, task: Any, pending: list[Any]) -> str:
        lines = [
            f"{PLAN_RUN_HEAD}{task.goal or '(未命名计划)'}",
            "本回合连续推进，直到计划完成或遇到需要用户决策的阻塞；不要中途停下询问是否继续。",
            "",
            "待完成条目：",
        ]
        lines.extend(self._item_lines(pending))
        lines += [
            "",
            "每完成一项用 todo_write 标 completed；标记前先自己跑一遍该条目的 verify。",
            "做不了就标 blocked 并说明缺什么。运行器会在本回合结束后统一机械验收。",
        ]
        return "\n".join(lines)

    def _continue_prompt(
        self, task: Any, pending: list[Any], failures: dict[str, str]
    ) -> str:
        lines = [f"{PLAN_CONTINUE_HEAD}{task.goal or '(未命名计划)'}"]
        if failures:
            lines += ["", "上一回合验收未通过，优先修复这些条目（只修失败点）："]
            for item_id, output in list(failures.items())[:5]:
                item = task.get(item_id)
                if item is not None:
                    lines.append(f"- {item.id}. {item.content}")
                    lines.append(f"  失败输出：{_tail(output)}")
        lines += ["", "仍未完成条目："]
        lines.extend(self._item_lines(pending))
        lines.append("继续推进；完成后用 todo_write 标 completed，不需要询问用户继续。")
        return "\n".join(lines)

    @staticmethod
    def _item_lines(items: list[Any]) -> list[str]:
        lines: list[str] = []
        for item in items[:10]:
            line = f"- {item.id}. {item.content}"
            if getattr(item, "verify", ""):
                line += f"（verify: {item.verify}）"
            lines.append(line)
        return lines

    # ---- settlement ---------------------------------------------------------

    async def _settle_completed(
        self, session: Any, task: Any, report: PlanReport, observer: Any
    ) -> dict[str, str]:
        """Verify every completed item this run has not settled yet.

        Returns the failed items' outputs: their status is reverted to
        ``in_progress`` and the next turn's prompt asks for a repair.  They are
        only marked ``blocked`` after the run's turn budget is spent.
        """
        failures: dict[str, str] = {}
        for item in list(task.items):
            current = task.get(item.id)
            if current is None or current.status != "completed":
                continue
            if (
                current.id in self._old_completed
                or current.id in self._recorded_this_run
            ):
                continue
            passed, verdicts, output = await self._verify_item(session, current)
            if current.verify and not passed:
                task.update(
                    current.id,
                    status="in_progress",
                    evidence=f"verify failed: {_tail(output)}",
                )
                self._emit_task(observer, task)
                failures[current.id] = output
                self._persist(session)
                continue
            summary = (
                f"{current.content}：verify passed"
                if current.verify
                else f"{current.content}：模型在分步之外标 done，原文照收"
            )
            delivery = StepDelivery(
                step_id=current.id,
                statement=current.content,
                status="completed",
                attempts=1 if current.verify else 0,
                ac=verdicts,
                summary=summary,
                attested=bool(current.verify),
            )
            self._recorded_this_run.add(current.id)
            report.deliveries.append(delivery)
            session.step_deliveries.append(delivery)
            if current.id not in report.completed:
                report.completed.append(current.id)
            self._emit_receipt(observer, delivery)
            self._emit_task(observer, task)
            self._persist(session)
        return failures

    async def _settle_open_leftovers(
        self,
        session: Any,
        task: Any,
        report: PlanReport,
        failures: dict[str, str],
        observer: Any,
    ) -> None:
        """Receipt what the run could not finish: blocked is a verdict, the rest
        is a stall (retryable next run)."""
        for item in list(task.items):
            current = task.get(item.id)
            if current is None or current.is_terminal() or current.status == "completed":
                continue
            if current.id in self._recorded_this_run or current.id in self._old_completed:
                continue
            if current.id in failures or current.status == "blocked":
                reason = failures.get(current.id, current.evidence)
                task.update(
                    current.id,
                    status="blocked",
                    evidence=f"verify failed: {_tail(reason)}",
                )
                self._emit_task(observer, task)
                delivery = StepDelivery(
                    step_id=current.id,
                    statement=current.content,
                    status="blocked",
                    attempts=1,
                    ac=[],
                    summary=f"{current.content}：{_tail(reason)}",
                    attested=False,
                )
                if current.id not in report.blocked:
                    report.blocked.append(current.id)
                self._skip_dependents(session, task, current, report, observer)
            else:
                delivery = StepDelivery(
                    step_id=current.id,
                    statement=current.content,
                    status="stalled",
                    attempts=max(1, int(getattr(current, "attempts", 0) or 0)),
                    ac=[],
                    summary=f"{current.content}：模型未完成（下次可重跑）",
                    attested=False,
                )
                if current.id not in report.stalled:
                    report.stalled.append(current.id)
            self._recorded_this_run.add(current.id)
            report.deliveries.append(delivery)
            session.step_deliveries.append(delivery)
            self._emit_receipt(observer, delivery)
            self._persist(session)

    async def _verify_item(
        self, session: Any, item: Any
    ) -> tuple[bool, list[dict[str, Any]], str]:
        if not item.verify:
            return True, [], ""
        contracts = [
            AcceptanceItem(
                id=f"plan-{item.id}", statement=item.content, verify=item.verify
            )
        ]
        session.acceptance = contracts
        try:
            await self._loop.verify_acceptance(session, None)
            passed = self._contracts_passed(session)
            verdicts = [
                {"id": entry.id, "passed": entry.passed} for entry in contracts
            ]
            output = self._verify_output(session)
        finally:
            session.acceptance = []
            self._persist(session)
        return passed, verdicts, output

    def _skip_dependents(
        self, session: Any, task: Any, item: Any, report: PlanReport, observer: Any
    ) -> None:
        skipped = task.skip_dependents(item.id, ts=now_utc())
        if skipped:
            self._emit_task(observer, task)
        for skipped_id in skipped:
            skipped_item = task.get(skipped_id)
            delivery = StepDelivery(
                step_id=skipped_id,
                statement=(skipped_item.content if skipped_item is not None else ""),
                status="skipped",
                attempts=0,
                ac=[],
                summary=f"依赖 #{item.id} 失败，跳过此项",
                attested=False,
            )
            self._recorded_this_run.add(skipped_id)
            report.deliveries.append(delivery)
            session.step_deliveries.append(delivery)
            if skipped_id not in report.skipped:
                report.skipped.append(skipped_id)
            self._emit_receipt(observer, delivery)
        if skipped:
            self._persist(session)

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _verdict_count(report: PlanReport) -> int:
        return (
            len(report.completed)
            + len(report.blocked)
            + len(report.stalled)
            + len(report.skipped)
        )

    @staticmethod
    def _states_changed(task: Any, before: dict[str, str]) -> bool:
        return any(
            (task.get(item_id) is not None)
            and task.get(item_id).status != status
            for item_id, status in before.items()
        )

    @staticmethod
    def _emit_receipt(observer: Any, delivery: StepDelivery) -> None:
        """One receipt event per settled item (the IPC contract counts them)."""
        if observer is None or not hasattr(observer, "on_event"):
            return
        try:
            observer.on_event(PlanStepReceipt(summary=delivery.summary))
        except Exception:  # noqa: BLE001 — observation must not cost the run
            pass

    def _force_build_posture(self, session: Any, observer: Any) -> None:
        """A boot resume can arrive in plan posture; execution is build work."""
        in_plan = (
            bool(getattr(session, "readonly", False))
            or str(getattr(session, "mode", "") or "") == "plan"
        )
        if not in_plan:
            return
        commit = getattr(self._loop, "commit_posture", None)
        if callable(commit):
            commit(
                session,
                agent="build",
                readonly=False,
                reason="plan run",
                observer=observer,
            )
            return
        session.agent = "build"
        session.readonly = False

    @staticmethod
    def _tool_calls(session: Any) -> int:
        return sum(
            1
            for message in getattr(session, "messages", ()) or ()
            if getattr(message, "role", "") == "tool"
        )
