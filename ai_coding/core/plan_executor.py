"""Plan-as-Goal executor (stage 2): rounds on the main session.

The approved plan is a durable objective.  ``/plan run`` drives it round by
round on the **parent** session instead of a fresh ephemeral step/wave session:
the conversation is the continuity, so an item never re-explains the plan and
never loses what the previous items learned.

Per round: arm the item's scope (``step_item_id``) and wall clock, run one
bounded turn with ``stop_policy="natural"``, then verify the item *after* the
turn through ``AgentLoop.verify_acceptance``.  Verification is deliberately
outside the turn: the acceptance ladder escalates to ``ask_user`` at L2, which
would turn an autonomous run back into a conversation.  A failed proof gets one
bounded repair turn; still failing means ``blocked`` and its dependents are
skipped.

Everything else is inherited from ``PlanRunner``: the workspace gate, the
arm/disarm lifecycle, receipts, delivery scoring, prompt markers and the
``RoundProfile`` hooks.  The only overridden pieces are the ones that assumed an
ephemeral step session.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from ai_coding.core.events import PlanStepReceipt
from ai_coding.core.plan_runner import STEP_HEAD, PlanReport, PlanRunner, _tail
from ai_coding.core.turn_policy import TurnPolicy
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.infra.deadline import Deadline
from ai_coding.infra.time_utils import now_utc

__all__ = ["PLAN_ROUND_CONTINUES", "PlanExecutor"]

#: The one line that tells the model the run continues without it.  It rides the
#: item prompt (``STEP_HEAD`` stays the replay marker).
PLAN_ROUND_CONTINUES = "运行器会在本回合结束后自动验收并进入下一项，不需要询问用户继续。"

#: A round that made no tool call and did not change the item is exploration,
#: not work; two in a row end the run (Codex's no-progress guard).
NO_PROGRESS_ROUNDS = 2


class PlanExecutor(PlanRunner):
    """The ``plan`` executor that keeps every round on the main session."""

    def __init__(self, loop: Any) -> None:
        super().__init__(loop)
        #: Completed receipts that existed before this run.  They are not
        #: settled again; a *stalled/blocked* receipt does not suppress a new
        #: completion.
        self._old_completed: set[str] = set()
        #: Receipts this run wrote, for the leftover sweep's dedupe.
        self._recorded_this_run: set[str] = set()
        #: The round's usage delta (``after_round`` adds it to the task ledger).
        self._round_usage: dict[str, int] | None = None
        #: Did the round call any tool or change the item's status?
        self._round_moved = False
        self._empty_rounds = 0
        #: ``finish`` is deferred: the post-run leftover sweep must finish before
        #: the report is rendered, or the final line undercounts what it settled.
        self._driver_finished = False

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
        self._driver_finished = False
        report = await super().run(
            session, console, on_delta=on_delta, observer=observer
        )
        task = getattr(session, "task", None)
        if task is not None and self._driver_finished:
            # A crash/approval can leave "completed but no receipt" entries.
            # Settle them *before* rendering: a verify item is checked, never
            # fiat-backfilled.  A refused/empty run never reaches the driver,
            # so it has no leftovers to settle.
            await self._settle_leftovers(session, task, report, observer)
            self._persist(session)
            self._emit_task(observer, task)
        if self._driver_finished:
            self._say(console, report.render())
        return report

    def prepare(self, session: Any, task: Any) -> set[str]:
        receipted = super().prepare(session, task)
        self._old_completed = {
            entry.step_id
            for entry in getattr(session, "step_deliveries", ()) or ()
            if entry.status == "completed"
        }
        self._recorded_this_run = set()
        self._empty_rounds = 0
        self._round_moved = False
        return receipted

    def before_round(
        self, task: Any, session: Any, report: PlanReport, receipted: set[str]
    ) -> None:
        # No fiat backfill here: a completed item without a receipt is settled
        # by ``_settle_leftovers``, which can verify it (the sync hook cannot).
        pass

    def no_item(self, task: Any, report: PlanReport) -> None:
        open_items = task.open_items()
        if not open_items:
            return
        never_attempted = [
            item.id for item in open_items if item.id not in self._run_attempted
        ]
        if never_attempted:
            report.stopped = (
                "deadlock: open items remain but none is ready "
                "(missing dependency or cycle)"
            )
        elif report.stopped is None:
            report.stopped = (
                f"stalled: {len(open_items)} open item(s) remain after this run"
            )

    def should_stop(self, report: PlanReport, item: Any) -> bool:
        return report.stopped is not None

    def after_round(self, session: Any, task: Any, report: PlanReport) -> None:
        # No ``rounds_run`` bump: it drives the plan card's "第 N 波", and a
        # session run has no waves.  The run's round count is ``report.turns_used``.
        if self._round_usage:
            task.plan_tokens_used = int(
                getattr(task, "plan_tokens_used", 0) or 0
            ) + int(self._round_usage.get("total_tokens", 0) or 0)
        budget = int(
            getattr(getattr(self._loop, "settings", None), "plan_token_budget", 0)
            or 0
        )
        if budget > 0 and int(getattr(task, "plan_tokens_used", 0) or 0) >= budget:
            report.stopped = (
                f"budget: plan token budget exhausted "
                f"({task.plan_tokens_used}/{budget})"
            )
        if self._round_moved:
            self._empty_rounds = 0
        else:
            self._empty_rounds += 1
        if (
            self._empty_rounds >= NO_PROGRESS_ROUNDS
            and task.open_items()
            and report.stopped is None
        ):
            report.stopped = (
                f"no progress: {self._empty_rounds} consecutive rounds made no "
                "tool call; remaining items stay open"
            )
        self._persist(session)

    def finish(self, console: Any, report: PlanReport) -> None:
        # ``run`` renders after the leftover sweep; rendering here would print a
        # report missing whatever that sweep settles.
        self._driver_finished = True

    # ---- one round -------------------------------------------------------

    async def run_round(
        self,
        session: Any,
        task: Any,
        item: Any,
        report: PlanReport,
        console: Any,
        *,
        on_delta: Any,
        observer: Any,
    ) -> None:
        self._say(
            console,
            f"[plan] 步骤 {item.id}/{len(task.items)}：{item.content}",
        )
        original_policy = self._loop.policy
        usage: dict[str, int] | None = None
        attempts = 0
        text = ""
        start_turn = int(getattr(session, "turn_count", 0) or 0)
        status_before = item.status
        tools_before = self._tool_calls(session)
        cap = max(
            1,
            int(
                getattr(
                    getattr(self._loop, "settings", None),
                    "plan_max_item_attempts",
                    2,
                )
                or 2
            ),
        )
        item_limit = float(
            getattr(
                getattr(self._loop, "settings", None), "plan_item_wall_seconds", 0.0
            )
            or 0.0
        )
        session.item_deadline = (
            Deadline.after(item_limit) if item_limit > 0 else None
        )
        session.step_item_id = str(getattr(item, "id", "") or "")
        session.acceptance = []
        contracts = self._contracts(item)
        self._loop.policy = TurnPolicy(
            replace(
                original_policy.config,
                max_iterations=max(1, self._step_budget()),
                stop_policy="natural",
                max_auto_continuations=1,
            )
        )
        try:
            text = await self._loop.process_input(
                session,
                self._step_prompt(item),
                on_delta=on_delta,
                observer=observer,
                synthetic=True,
            )
            attempts = 1
            usage = self._charge_turn(report, usage)
            if contracts:
                session.acceptance = contracts
                await self._loop.verify_acceptance(session, None)
                if not self._contracts_passed(session) and attempts < cap:
                    # The repair turn is the last attempt: one continuation per
                    # turn would otherwise stack another full iteration budget.
                    # No acceptance contract rides the turn (same rule as the
                    # first one); it is checked again afterwards.
                    failure = self._verify_output(session)
                    session.acceptance = []
                    self._loop.policy = TurnPolicy(
                        replace(
                            self._loop.policy.config,
                            max_auto_continuations=0,
                        )
                    )
                    text = await self._loop.process_input(
                        session,
                        self._repair_prompt_text(item, failure),
                        on_delta=on_delta,
                        observer=observer,
                        synthetic=True,
                    )
                    attempts = 2
                    usage = self._charge_turn(report, usage)
                    session.acceptance = contracts
                    await self._loop.verify_acceptance(session, None)
            delivery = self._settle(
                session,
                session,
                task,
                item,
                report,
                text,
                attempts,
                (start_turn, int(getattr(session, "turn_count", 0) or 0)),
                usage,
                0,
                observer=observer,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad item must not kill the plan
            task.update(item.id, status="blocked", evidence=f"runner error: {exc}")
            self._emit_task(observer, task)
            report.blocked.append(item.id)
            delivery = self._delivery(
                item,
                "blocked",
                1,
                [],
                f"runner error: {exc}",
                (start_turn, start_turn),
                usage,
                0,
            )
            report.deliveries.append(delivery)
            session.step_deliveries.append(delivery)
            self._recorded_this_run.add(item.id)
            self._emit_receipt(observer, delivery)
        finally:
            self._loop.policy = original_policy
            session.step_item_id = ""
            session.item_deadline = None
            session.acceptance = []
            # ``acceptance`` is persisted by ``SessionData.to_dict``; clearing it
            # in memory is not enough.  Persist the cleared copy so a later
            # resume cannot re-open an old contract.
            self._persist(session)

        self._recorded_this_run.add(delivery.step_id)
        self._emit_receipt(observer, delivery)
        if delivery.status == "blocked":
            self._skip_dependents(session, task, item, report, observer)

        current = task.get(item.id)
        self._round_moved = bool(
            self._tool_calls(session) > tools_before
            or (current is not None and current.status != status_before)
        )
        self._round_usage = usage
        # The turn may have finished items beyond the one it was asked about;
        # settle those now, with their own proofs.
        await self._settle_leftovers(session, task, report, observer)

    # ---- settle leftovers (extras / crash residue) -----------------------

    async def _settle_leftovers(
        self, session: Any, task: Any, report: PlanReport, observer: Any
    ) -> None:
        """Receipt every completed item this run has not settled yet.

        A verify item is checked here (the sync ``before_round`` hook cannot run
        a command), and a failure blocks it and skips its dependents.  An item
        without a proof command is fiat: the model claimed it outside a bounded
        turn, so nothing above vouches for it (``attested=False``).
        """
        for item in list(task.items):
            current = task.get(item.id)
            if current is None or current.status != "completed":
                continue
            if (
                current.id in self._old_completed
                or current.id in self._recorded_this_run
            ):
                continue
            verified, verdicts, output = await self._verify_extra(session, current)
            if current.verify and not verified:
                task.update(
                    current.id,
                    status="blocked",
                    evidence=f"verify failed on resume: {_tail(output)}",
                )
                self._emit_task(observer, task)
                report.blocked.append(current.id)
                delivery = StepDelivery(
                    step_id=current.id,
                    statement=current.content,
                    status="blocked",
                    attempts=0,
                    ac=verdicts,
                    summary=f"{current.content}：断点补验失败（{_tail(output)}）",
                    attested=False,
                )
                self._recorded_this_run.add(current.id)
                report.deliveries.append(delivery)
                session.step_deliveries.append(delivery)
                self._emit_receipt(observer, delivery)
                self._skip_dependents(session, task, current, report, observer)
            else:
                summary = (
                    f"{current.content}：断点补验通过"
                    if current.verify
                    else f"{current.content}：模型在分步之外标 done，原文照收"
                )
                delivery = StepDelivery(
                    step_id=current.id,
                    statement=current.content,
                    status="completed",
                    attempts=1,
                    ac=verdicts,
                    summary=summary,
                    attested=bool(current.verify),
                )
                self._recorded_this_run.add(current.id)
                report.deliveries.append(delivery)
                session.step_deliveries.append(delivery)
                self._emit_receipt(observer, delivery)
                if current.id not in report.completed:
                    report.completed.append(current.id)
                self._emit_task(observer, task)
            self._persist(session)

    async def _verify_extra(
        self, session: Any, item: Any
    ) -> tuple[bool, list[dict[str, Any]], str]:
        """Run one leftover item's proof command, never a fiat shortcut."""
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

    # ---- skip propagation ------------------------------------------------

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
            self._emit_receipt(observer, delivery)
            if skipped_id not in report.skipped:
                report.skipped.append(skipped_id)
        if skipped:
            self._persist(session)

    # ---- helpers ---------------------------------------------------------

    def _contracts(self, item: Any) -> list[AcceptanceItem]:
        if not item.verify:
            return []
        return [
            AcceptanceItem(
                id=f"plan-{item.id}", statement=item.content, verify=item.verify
            )
        ]

    def _step_prompt(self, item: Any) -> str:
        return f"{super()._step_prompt(item)}\n（{PLAN_ROUND_CONTINUES}）"

    def _repair_prompt_text(self, item: Any, failure: str) -> str:
        """The repair turn's prompt, with the failure quoted from before the
        contract was cleared (``_repair_prompt`` would read cleared acceptance)."""
        return (
            f"{STEP_HEAD}{item.content}\n"
            "上一回合验收未通过，只修复失败点，不要重做已通过的部分：\n"
            f"{_tail(failure)}"
        )

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
    def _emit_receipt(observer: Any, delivery: StepDelivery) -> None:
        """One receipt event per settled item (the IPC contract counts them)."""
        if observer is None or not hasattr(observer, "on_event"):
            return
        try:
            observer.on_event(PlanStepReceipt(summary=delivery.summary))
        except Exception:  # noqa: BLE001 — observation must not cost the run
            pass

    @staticmethod
    def _tool_calls(session: Any) -> int:
        return sum(
            1
            for message in getattr(session, "messages", ()) or ()
            if getattr(message, "role", "") == "tool"
        )
