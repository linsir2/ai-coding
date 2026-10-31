"""Wave runner (r24/C2): a plan runs in waves of one continuous session.

A wave is a slice of the open list (see ``core/wave_planner.py``).  Inside it
several items share one ephemeral session, so the model keeps what it just read
and decided; between waves a fresh session starts from a mechanical handoff
(the changed files, the completed/skipped items, the proofs that passed), which
is also re-rendered into every request as ``task.wave_note`` so compaction
cannot drop it.

The loop is the existing one: ``TaskDriver`` drives one round per wave, and
each item is still an ordinary ``process_input`` turn.  What this module owns is
the boundary work the loop must not do:

* which item is next (dependency readiness inside the wave);
* the acceptance contract for that item (the loop verifies it when the model
  stops, and the acceptance ladder repairs inside the same turn);
* settling every item the model finished in that turn (it may finish several);
* one receipt per item, a ``skipped`` receipt for each dependent given up;
* the wave handoff, the wave/episode budgets, and crash-point persistence.

It never mutates the loop's policy while a turn runs, never assumes one item per
turn, and never lets a blocked item end the wave while independent work remains.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from ai_coding.core.events import PlanStepReceipt, TaskUpdated
from ai_coding.core.plan_runner import (
    STEP_HEAD,
    PlanReport,
    PlanRunner,
    outside_workspace_message,
    outside_workspace_paths,
)
from ai_coding.core.task_driver import TaskDriver
from ai_coding.core.turn_policy import TurnPolicy
from ai_coding.core.wave_planner import (
    DEFAULT_WAVE_SIZE,
    Wave,
    group_waves,
    wave_is_open,
)
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.domain.plan_wave import WaveHandoff
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import WAVE_NOTE_MAX_CHARS, TaskState, TodoItem
from ai_coding.infra.deadline import Deadline
from ai_coding.infra.time_utils import now_utc

__all__ = ["WaveRunner", "WaveItem", "WAVE_ITEM_HEAD", "plan_runner_for"]


def plan_runner_for(loop: Any, user_prompt: Any = None) -> Any:
    """The executor the configuration asks for (``session`` unless told otherwise).

    One selection point for the command layer and the server:

    * ``session`` (default) — Plan-as-Goal: every round runs on the parent
      session, so the plan's transcript is the continuity (stage-2 default);
    * ``wave`` — the context-bounded executor (fresh session per wave, handoff
      between waves; the scale path for very large plans);
    * ``legacy`` — the pre-wave runner, kept for its tests and rollback.

    ``user_prompt`` (C3) is the human channel a wave-boundary replan asks for
    approval through.  ``None`` means the run cannot get approval, so a replan
    is rolled back instead of applied -- the conservative direction.
    """
    settings = getattr(loop, "settings", None)
    name = str(getattr(settings, "plan_orchestrator", "session") or "session")
    if name == "legacy":
        from ai_coding.core.plan_runner import PlanRunner

        return PlanRunner(loop)
    if name == "wave":
        return WaveRunner(loop, user_prompt=user_prompt)
    from ai_coding.core.plan_executor import PlanExecutor

    return PlanExecutor(loop)


#: The per-item prompt's leading marker.  Reuses the legacy step head on
#: purpose: the replay and the tests key on it, and a wave item is still "one
#: item, then stop for verification".  The wave context lives in the seed.
WAVE_ITEM_HEAD = STEP_HEAD

#: Failure output quoted into a repair prompt (same 500-char preview discipline
#: as the acceptance evidence tails).
_OUTPUT_TAIL = 500


def _tail(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _OUTPUT_TAIL else f"…{text[-_OUTPUT_TAIL:]}"


@dataclass(frozen=True)
class WaveItem:
    """One wave as the driver's "item" — it needs an id and an order.

    ``replan`` is non-empty when this "round" is the wave-boundary replan turn
    instead of a real wave (C3); ``wave`` is then empty.
    """

    id: str
    wave: Wave
    replan: str = ""


class WaveRunner:
    """The ``plan`` RoundProfile that executes a plan wave by wave."""

    def __init__(self, loop: Any, user_prompt: Any = None) -> None:
        self._loop = loop
        self._parent: Any = None
        self._task: TaskState | None = None
        self._observer: Any = None
        self._attempted_items: set[str] = set()
        self._receipted: set[str] = set()
        self._recorded_this_run: set[str] = set()
        self._wave_tokens: int = 0
        self._wave_started: float = 0.0
        self._base_config: Any = None
        # C3: the human channel a replan asks for approval through (None means
        # nobody can approve, so a replan is rolled back).
        self._user_prompt = user_prompt
        #: A wave asked for a replan; the next round must be that turn.
        self._replan_pending: str = ""
        #: Replan turns opened this run (the budget counts attempts).
        self._replans_opened: int = 0
        #: The last round was a real wave (``after_round`` updates the wave
        #: counter only then -- a replan is not a wave).
        self._last_round_was_wave: bool = True
        #: C3/A+C: the only automatic brake on a run that has no per-wave
        #: spend cap.  A wave that completes nothing is exploration, not work;
        #: consecutive such waves are the failure mode worth stopping.
        self._waves_without_progress: int = 0
        self._completed_seen: int = 0

    # ---- lifecycle --------------------------------------------------------

    async def run(
        self,
        session: Any,
        console: Any = None,
        *,
        on_delta: Any = None,
        observer: Any = None,
    ) -> PlanReport:
        """Run the plan in waves; arm/persist/disarm like every armed task."""
        self._observer = observer
        task = getattr(session, "task", None)
        offenders = self._workspace_offenders(task)
        if offenders:
            refused = PlanReport()
            refused.stopped = outside_workspace_message(offenders)
            if task is not None and task.disarm():
                self._persist(session)
                self._emit_task(task)
            return refused
        if task is not None:
            task.armed = True
            task.armed_profile = "plan"
            self._persist(session)
            self._emit_task(task)
            self._receipted = {
                entry.step_id for entry in getattr(session, "step_deliveries", ())
            }
            await self._backfill_receipts(session, task, observer)
        try:
            report: PlanReport = await TaskDriver(self._loop).run(
                session, self, console, on_delta=on_delta, observer=observer
            )
            return report
        finally:
            if task is not None:
                if task.disarm():
                    self._persist(session)
                self._emit_task(task)

    # ---- RoundProfile ----------------------------------------------------

    def new_report(self) -> PlanReport:
        return PlanReport()

    def max_rounds(self) -> int:
        """Wave cap for one run; 0 (default) disables it."""
        return self._setting_int("plan_max_waves", 0)

    def prepare(self, session: Any, task: Any) -> set[str]:
        self._parent = session
        self._task = task
        # Same receipt hygiene as the legacy runner: a list swapped mid-turn
        # leaves receipts whose ids collide with the new list's ids.
        PlanRunner._drop_foreign_receipts(session, task)
        self._receipted = {entry.step_id for entry in getattr(session, "step_deliveries", [])}
        self._attempted_items = set()
        self._recorded_this_run = set()
        self._replan_pending = ""
        self._replans_opened = 0
        self._last_round_was_wave = True
        # A new run starts with a clean progress ledger; the counters describe
        # *this* run's waves, not the task's history.
        self._waves_without_progress = 0
        self._completed_seen = 0
        return self._receipted

    def next_item(self, task: Any, attempted: set[str]) -> WaveItem | None:
        # C3: a wave asked for a replan -> that turn runs before any new wave.
        # A finished plan (no open items) has nothing to rewrite: the request
        # is dropped rather than spending a turn on an empty list.
        if self._replan_pending:
            if task.open_items() and self._can_replan():
                return self._replan_item(self._replan_pending)
            self._replan_pending = ""
        waves = group_waves(task, size=self._setting_int("plan_wave_size", DEFAULT_WAVE_SIZE))
        for wave in waves:
            signature = self._wave_signature(wave)
            if signature in attempted:
                continue
            if not wave_is_open(task, wave):
                continue
            if self._next_ready(task, wave) is None:
                # Every item in this wave is waiting on something; running the
                # wave would spend a round to do nothing.
                continue
            return WaveItem(id=signature, wave=wave)
        # C3: no runnable wave while an open item was never attempted = deadlock
        # (a missing dependency or a cycle).  Items this run already tried and
        # left open are "stalled", not deadlocked -- C2's ending stays.
        if self._deadlocked(task) and self._can_replan():
            return self._replan_item(
                "deadlock: 剩余项没有可运行的波（依赖缺失或成环）"
            )
        return None

    def _can_replan(self) -> bool:
        return self._replans_opened < self._setting_int("plan_max_replans", 2)

    def _deadlocked(self, task: TaskState) -> bool:
        open_items = task.open_items()
        return bool(open_items) and any(
            item.id not in self._attempted_items for item in open_items
        )

    def _replan_item(self, reason: str) -> WaveItem:
        """Consume the pending replan and hand the driver one round for it."""
        self._replan_pending = ""
        self._replans_opened += 1
        return WaveItem(
            id=f"replan-{self._replans_opened}",
            wave=Wave(index=0, items=()),
            replan=reason,
        )

    def no_item(self, task: Any, report: PlanReport) -> None:
        open_items = task.open_items()
        if not open_items:
            return
        never_attempted = [item.id for item in open_items if item.id not in self._attempted_items]
        if never_attempted:
            report.stopped = (
                "deadlock: open items remain but none is ready "
                "(missing dependency or cycle)"
            )
        elif report.stopped is None:
            report.stopped = (
                f"stalled: {len(open_items)} open item(s) remain after this run"
            )

    def rounds_used(self, report: PlanReport) -> int:
        return report.waves

    def budget_stop(self, report: PlanReport, cap: int) -> None:
        report.stopped = (
            f"budget: plan wave cap reached ({report.waves}/{cap} waves); "
            "remaining items stay open"
        )

    def before_round(
        self, task: Any, session: Any, report: PlanReport, receipted: set[str]
    ) -> None:
        pass

    async def run_round(
        self,
        session: Any,
        task: Any,
        item: WaveItem,
        report: PlanReport,
        console: Any,
        *,
        on_delta: Any,
        observer: Any,
    ) -> None:
        if item.replan:
            self._last_round_was_wave = False
            await self._run_replan(
                session, task, item, report, console,
                on_delta=on_delta, observer=observer,
            )
            return
        self._last_round_was_wave = True
        await self._run_wave(
            session, task, item.wave, report, console,
            on_delta=on_delta, observer=observer,
        )

    def after_round(self, session: Any, task: Any, report: PlanReport) -> None:
        # A replan turn is not a wave: the wire's ``roundsRun`` (and the card's
        # "第 N 波") must keep counting only real waves.
        if self._last_round_was_wave:
            task.rounds_run = int(getattr(task, "rounds_run", 0) or 0) + 1
            self._settle_progress(task, report)
        self._persist(session)

    def _settle_progress(self, task: TaskState, report: PlanReport) -> None:
        """Count waves that finished an item; stop after too many empty ones.

        A+C: per-wave token/wall budgets are off by default (a real item costs
        more than the old cap), so this is the one automatic brake left.  It
        keys on *completed* items -- the only progress the plan can bank --
        and never fires while the run is still finishing work each wave.
        """
        completed = len(report.completed)
        if completed > self._completed_seen:
            self._waves_without_progress = 0
        else:
            self._waves_without_progress += 1
        self._completed_seen = completed
        cap = self._setting_int("plan_no_progress_waves", 3)
        if cap and self._waves_without_progress >= cap and task.open_items():
            report.stopped = (
                f"no progress: {self._waves_without_progress} consecutive waves "
                "completed no item; remaining items stay open"
            )

    def should_stop(self, report: PlanReport, item: WaveItem) -> bool:
        return report.stopped is not None

    def taskless(self, report: PlanReport, console: Any) -> None:
        report.stopped = "no-open-items"
        self._say(console, "[plan] 完成 0 步 · 用了 0 波（无任务项）")

    def persist(self, session: Any) -> None:
        self._persist(session)

    def finish(self, console: Any, report: PlanReport) -> None:
        self._say(console, report.render())

    # ---- one wave ---------------------------------------------------------

    async def _backfill_receipts(
        self, session: Any, task: TaskState, observer: Any
    ) -> None:
        """Re-verify items a crash left ``completed`` without a receipt (P0-5).

        The wave session is ephemeral, so a crash between the model marking an
        item done and the receipt landing leaves the parent file saying
        "completed" with no evidence.  On resume the item is never picked again
        (terminal) -- this closes that hole: the proof is re-run, a failure
        reopens it as blocked, and a pass writes a ``attested=False`` receipt
        saying where the completion came from.
        """
        missing = [
            item for item in task.items
            if item.status == "completed" and item.id not in self._receipted
        ]
        if not missing:
            return
        probe = SessionData(
            session_id=f"plan-probe-{uuid.uuid4().hex[:12]}",
            title="plan backfill",
            created_time=now_utc(),
            last_access_time=now_utc(),
        )
        probe.ephemeral = True
        probe.task = task
        probe.permissions = [dict(row) for row in getattr(session, "permissions", ()) or ()]
        report = PlanReport()
        changed = False
        for item in missing:
            # These came from a previous run; this run must not open a new turn
            # for them (a failed re-verify stays blocked, not retried now).
            self._attempted_items.add(item.id)
            passed = True
            output = ""
            if item.verify:
                probe.acceptance = [
                    AcceptanceItem(
                        id=f"plan-{item.id}", statement=item.content, verify=item.verify
                    )
                ]
                await self._loop.verify_acceptance(probe)
                passed = bool(probe.acceptance) and all(
                    entry.passed is True for entry in probe.acceptance
                )
                output = next(
                    (str(entry.output) for entry in probe.acceptance if entry.output), ""
                )
                probe.acceptance = []
            changed = True
            if not passed:
                task.update(
                    item.id,
                    status="blocked",
                    evidence=f"verify failed on resume: {_tail(output)}",
                )
                self._emit_task(task)
                delivery = self._delivery(
                    item, "blocked", 0, False,
                    f"{item.content}：断点补验失败（{_tail(output)}）",
                    (0, 0), None, 0,
                )
                delivery.attested = False
                self._record(report, session, delivery, observer=observer, bubbles=False)
                self._skip_dependents(session, task, item, report, observer)
            else:
                delivery = self._delivery(
                    item, "completed", 0, bool(item.verify),
                    f"{item.content}：断点补验通过",
                    (0, 0), None, 0,
                )
                delivery.attested = False
                self._record(report, session, delivery, observer=observer, bubbles=False)
        if changed:
            self._persist(session)
            self._emit_task(task)

    async def _run_wave(
        self,
        parent: Any,
        task: TaskState,
        wave: Wave,
        report: PlanReport,
        console: Any,
        *,
        on_delta: Any,
        observer: Any,
    ) -> None:
        report.waves += 1
        self._wave_tokens = 0
        deliveries_start = len(report.deliveries)
        # C3: the wave is live now -- wave_report becomes callable, the card can
        # say which wave is running, and a stale report from an earlier wave is
        # dropped.
        task.wave_active = True
        task.wave_items = wave.ids
        task.wave_report = None
        self._emit_task(task)
        seed = self._seed(task, wave, report)
        wave_session = self._make_wave_session(parent, task, seed, report)
        started = time.monotonic()
        self._wave_started = started
        original_policy = self._loop.policy
        self._base_config = original_policy.config
        attempts_cap = self._attempts_cap()
        self._say(console, f"[plan] 第 {report.waves} 波（{len(wave.items)} 项）")
        completed = False
        try:
            # A previous turn (or the planning turn) may already have marked
            # items completed; give them their proof/receipt before picking work.
            await self._settle_extra_completions(
                wave_session, parent, task, wave, report, observer,
            )
            while True:
                ready = self._next_ready(task, wave)
                if ready is None:
                    break
                if report.stopped:
                    break
                if self._episode_budget_spent(task):
                    report.stopped = (
                        f"budget: plan token budget exhausted "
                        f"({task.plan_tokens_used}/{self._setting_int('plan_token_budget', 0)})"
                    )
                    break
                if self._wave_budget_spent():
                    report.stopped = self._wave_budget_reason()
                    break
                if self._wave_time_spent(started):
                    report.stopped = self._wave_time_reason()
                    break

                item = self._current(task, ready)
                if item is None:
                    break
                self._attempted_items.add(item.id)
                # One wall clock per item, shared by every turn the attempts
                # spend on it (see ``plan_item_wall_seconds``).  Cleared after
                # the item so the settle turn that follows is unscoped.
                self._arm_item_deadline(wave_session)
                try:
                    _moved, usage = await self._run_item(
                        wave_session, parent, task, wave, item, report,
                        console, on_delta=on_delta, observer=observer,
                        attempts_cap=attempts_cap,
                    )
                finally:
                    wave_session.item_deadline = None
                if usage:
                    report.tokens_used = self._merge_usage(report.tokens_used, usage)
                # The model may have finished more than the item it was asked
                # about; settle them before choosing the next one.
                await self._settle_extra_completions(
                    wave_session, parent, task, wave, report, observer,
                )
            # The last turn may have completed an item in a later wave; settle
            # it before the wave's handoff is written.
            await self._settle_extra_completions(
                wave_session, parent, task, wave, report, observer,
            )
            completed = True
        finally:
            self._loop.policy = original_policy
            if not completed:
                # Esc/cancel (or any exception) skips the wave-end block below.
                # These facts are transient, but the run's closing emit would
                # otherwise advertise a wave that no longer exists, and a reused
                # task would keep wave_report advertised.
                task.wave_active = False
                task.wave_items = ()
                task.wave_report = None

        task.wave_note = self._build_wave_note(wave_session, task, wave)
        task.wave_handoff = self._build_wave_handoff(
            wave_session, task, wave, report.deliveries[deliveries_start:]
        ).to_dict()
        # C3: a model that asked for a replan gets one *before* the next wave
        # (the trigger is the wave_report, not a blocked ratio).
        if isinstance(task.wave_report, dict) and task.wave_report.get("needs_replan"):
            self._replan_pending = "needs_replan: 模型请求重规划剩余项"
        task.wave_report = None
        task.wave_active = False
        task.wave_items = ()
        task.plan_tokens_used = int(getattr(task, "plan_tokens_used", 0) or 0) + self._wave_tokens
        self._persist(parent)
        self._emit_task(task)
        # The wave's transcript is deliberately disposable (same contract as a
        # plan step): the receipts and the handoff are what survives.

    # ---- one item ---------------------------------------------------------

    async def _run_item(
        self,
        wave_session: SessionData,
        parent: Any,
        task: TaskState,
        wave: Wave,
        item: TodoItem,
        report: PlanReport,
        console: Any,
        *,
        on_delta: Any,
        observer: Any,
        attempts_cap: int,
    ) -> tuple[bool, dict[str, int] | None]:
        """Work one item until it is verified, given up, or out of attempts.

        Verification items get the repair loop (one bounded retry after a failed
        proof).  Items **without** a proof command do not: a model that left one
        open is recorded as ``stalled`` and retried next run, never retried
        inside this run -- the legacy runner's measured rule ("a step the model
        left open without proof commands is stalled, never silently retried").
        Spending a second turn on a forgotten item costs the whole wave its
        budget and teaches the model nothing.
        """
        attempts = int(getattr(item, "attempts", 0) or 0)
        usage: dict[str, int] | None = None
        failure = ""
        start_turn = int(getattr(wave_session, "turn_count", 0) or 0)
        started_tools = self._tool_calls(wave_session)
        if not item.verify:
            attempts += 1
            item.attempts = attempts
            moved, usage = await self._one_turn(
                wave_session, item, report, prompt=self._item_prompt(item),
                on_delta=on_delta, observer=observer,
            )
            current = task.get(item.id)
            if current is not None and current.status == "completed":
                self._complete_item(parent, task, wave_session, item, report,
                                    attempts, start_turn, len(wave_session.messages))
            elif current is not None and current.status == "blocked":
                self._block_item(parent, task, wave_session, item, report, attempts,
                                 start_turn, "model reported blocked", observer)
            else:
                self._stall_item(parent, wave_session, item, report, attempts,
                                 start_turn, observer)
            return moved, usage
        while True:
            attempts += 1
            item.attempts = attempts
            # The first attempt may continue once; the repair attempt may not
            # (C3 is per turn, so the repair would buy another full round --
            # see ``_set_policy``).
            self._set_policy(
                item, wave_session, continuations=1 if attempts == 1 else 0
            )
            self._set_acceptance(wave_session, item)
            prompt = self._item_prompt(item, repair=attempts > 1, failure=failure)
            try:
                await self._loop.process_input(
                    wave_session, prompt, on_delta=on_delta, observer=observer,
                    synthetic=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad turn must not kill the wave
                failure = f"runner error: {exc}"
                break
            report.turns_used += 1
            turn_usage = self._last_usage()
            usage = self._merge_usage(usage, turn_usage)
            self._wave_tokens += self._total_tokens(turn_usage)
            if self._item_passed(wave_session, item):
                self._complete_item(parent, task, wave_session, item, report,
                                    attempts, start_turn, len(wave_session.messages))
                return self._tool_calls(wave_session) > started_tools, usage
            failure = self._acceptance_output(wave_session) or self._last_text()
            if self._wave_budget_spent():
                report.stopped = self._wave_budget_reason()
                return self._tool_calls(wave_session) > started_tools, usage
            if self._wave_time_spent(self._wave_started):
                report.stopped = self._wave_time_reason()
                return self._tool_calls(wave_session) > started_tools, usage
            if attempts < attempts_cap and not report.stopped:
                continue
            break
        if report.stopped:
            # The budget cut this attempt, it did not fail it: the item stays
            # open for the next wave/run instead of being called blocked.
            return self._tool_calls(wave_session) > started_tools, usage
        self._block_item(parent, task, wave_session, item, report, attempts,
                         start_turn, failure, observer)
        return self._tool_calls(wave_session) > started_tools, usage

    async def _one_turn(
        self,
        wave_session: SessionData,
        item: TodoItem,
        report: PlanReport,
        *,
        prompt: str,
        on_delta: Any,
        observer: Any,
    ) -> tuple[bool, dict[str, int] | None]:
        """Run one turn for an item with no proof contract (no repair loop)."""
        self._set_policy(item, wave_session)
        self._set_acceptance(wave_session, item)
        before = self._tool_calls(wave_session)
        try:
            await self._loop.process_input(
                wave_session, prompt, on_delta=on_delta, observer=observer,
                synthetic=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the caller settles the failure
            return False, None
        report.turns_used += 1
        usage = self._last_usage()
        self._wave_tokens += self._total_tokens(usage)
        return self._tool_calls(wave_session) > before, usage

    def _complete_item(
        self,
        parent: Any,
        task: TaskState,
        wave_session: SessionData,
        item: TodoItem,
        report: PlanReport,
        attempts: int,
        start_turn: int,
        seed_chars: int,
    ) -> None:
        if not item.is_terminal():
            evidence = (
                f"verify passed: {_tail(self._acceptance_output(wave_session))}"
                if item.verify
                else "model completed"
            )
            task.update(item.id, status="completed", evidence=evidence)
        elif item.status != "completed":
            task.update(item.id, status="completed", evidence="verify passed")
        self._emit_task(task)
        delivery = self._delivery(
            item, "completed", attempts,
            bool(self._acceptance_passed(wave_session, item)),
            self._summary(item),
            (start_turn, int(getattr(wave_session, "turn_count", 0) or 0)),
            self._last_usage(),
            seed_chars,
        )
        self._record(report, parent, delivery, observer=self._observer)
        # The wave session is ephemeral, but the task it shares with the parent
        # is the durable record.  Saving only at the wave boundary meant a run
        # killed mid-wave left every finished item ``pending`` on disk (measured
        # 2026-10-30: items 2-4 completed in memory, the file still said item 1).
        self._persist(parent)

    def _block_item(
        self,
        parent: Any,
        task: TaskState,
        wave_session: SessionData,
        item: TodoItem,
        report: PlanReport,
        attempts: int,
        start_turn: int,
        failure: str,
        observer: Any,
    ) -> None:
        reason = (
            f"verify failed: {_tail(failure)}"
            if item.verify
            else f"模型未完成本项: {_tail(failure) or 'no evidence'}"
        )
        task.update(item.id, status="blocked", evidence=reason)
        self._emit_task(task)
        report.blocked.append(item.id)
        delivery = self._delivery(
            item, "blocked", attempts,
            False, f"{item.content}：{_tail(reason)}",
            (start_turn, int(getattr(wave_session, "turn_count", 0) or 0)),
            self._last_usage(),
            len(wave_session.messages),
        )
        self._record(report, parent, delivery, observer=observer)
        self._persist(parent)  # a blocked item is a durable verdict too
        self._skip_dependents(parent, task, item, report, observer)

    def _stall_item(
        self,
        parent: Any,
        wave_session: SessionData,
        item: TodoItem,
        report: PlanReport,
        attempts: int,
        start_turn: int,
        observer: Any,
    ) -> None:
        """Record an item the model left open; keep it retryable next run.

        No task mutation: the item's own status (pending/in_progress) is the
        truth, and a blocked claim went through ``_block_item`` instead.  The
        delivery is what makes the run's report honest about the untouched
        item.
        """
        delivery = self._delivery(
            item, "stalled", attempts, False,
            f"{item.content}：模型未完成（下次可重跑）",
            (start_turn, int(getattr(wave_session, "turn_count", 0) or 0)),
            self._last_usage(),
            len(wave_session.messages),
        )
        self._record(report, parent, delivery, observer=observer)
        self._persist(parent)  # the receipt is what /resume reads

    def _skip_dependents(
        self, parent: Any, task: TaskState, item: TodoItem, report: PlanReport, observer: Any
    ) -> list[str]:
        skipped = task.skip_dependents(item.id, ts=now_utc())
        if skipped:
            self._emit_task(task)
        for skipped_id in skipped:
            skipped_item = task.get(skipped_id)
            delivery = StepDelivery(
                step_id=skipped_id,
                statement=(skipped_item.content if skipped_item is not None else ""),
                status="skipped",
                attempts=0,
                ac=[],
                summary=f"依赖 #{item.id} 失败，跳过此项",
                turns=(0, 0),
                attested=False,
            )
            self._record(report, parent, delivery, observer=observer)
        return skipped

    async def _settle_extra_completions(
        self,
        wave_session: SessionData,
        parent: Any,
        task: TaskState,
        wave: Wave,
        report: PlanReport,
        observer: Any,
    ) -> None:
        """Settle items the model finished while working on another one.

        Scans the **whole task**, not only the current wave: a model that marks
        a later wave's item completed gets no dedicated turn (``TaskDriver``
        skips terminal items), so without this sweep that item has no proof and
        no receipt -- the measured 3-item run reported 2/3 while the task said
        3/3.  An item already carrying a completed receipt is never settled
        twice (a resumed task has receipts from an earlier run).
        """
        receipted = {
            entry.step_id
            for entry in getattr(parent, "step_deliveries", ()) or ()
            if entry.status == "completed"
        }
        for item in task.items:
            current = task.get(item.id)
            if current is None or current.status != "completed":
                continue
            if current.id in self._recorded_this_run or current.id in receipted:
                continue
            if current.verify:
                # The tracked item's contract was checked at the turn's stop;
                # an extra item needs its own check, outside the turn.
                wave_session.acceptance = [
                    AcceptanceItem(
                        id=f"plan-{current.id}",
                        statement=current.content,
                        verify=current.verify,
                    )
                ]
                await self._loop.verify_acceptance(wave_session)
                if not self._acceptance_passed(wave_session, current):
                    task.update(
                        current.id,
                        status="in_progress",
                        evidence=f"verify failed: {_tail(self._acceptance_output(wave_session))}",
                    )
                    self._emit_task(task)
                    continue
                task.update(
                    current.id,
                    status="completed",
                    evidence=f"verify passed: {_tail(self._acceptance_output(wave_session))}",
                )
                self._emit_task(task)
                delivery = self._delivery(
                    current, "completed", 1,
                    True,
                    self._summary(current),
                    (0, int(getattr(wave_session, "turn_count", 0) or 0)),
                    self._last_usage(),
                    len(wave_session.messages),
                )
            else:
                # Fiat: the model claimed it, no command can prove it.  Mark it
                # like the legacy backfill -- not attested by an回合, zero turns.
                delivery = self._delivery(
                    current, "completed", 0, False,
                    self._summary(current), (0, 0), None,
                    len(wave_session.messages), attested=False,
                )
            self._record(report, parent, delivery, observer=observer)

    # ---- session / prompt / acceptance ------------------------------------

    def _make_wave_session(
        self, parent: Any, task: TaskState, seed: str, report: PlanReport
    ) -> SessionData:
        session = SessionData(
            session_id=f"plan-wave-{uuid.uuid4().hex[:12]}",
            title=f"plan wave {report.waves}",
            created_time=now_utc(),
            last_access_time=now_utc(),
        )
        session.ephemeral = True
        session.task = task
        # The parent's session rules must reach the wave, or every "allow for
        # this session" the operator gave is lost at the first boundary.
        session.permissions = [dict(row) for row in getattr(parent, "permissions", ()) or ()]
        session.agent = "build"
        session.readonly = False
        session.messages = [
            ChatMessage(role="user", content=seed, timestamp="", synthetic=True)
        ]
        return session

    def _seed(self, task: TaskState, wave: Wave, report: PlanReport) -> str:
        lines = [f"[计划标题] {task.goal or '(未命名计划)'}"]
        handoff = WaveHandoff.from_dict(task.wave_handoff)
        if handoff is not None:
            # C3: the structured handoff, once per wave (the seed is built once).
            # A wave that produced none falls back to the mechanical note below.
            lines += ["", "[上一波交接]", handoff.render()]
        elif task.wave_note:
            lines += ["", "[上一波交接]", task.wave_note.strip()]
        lines += ["", f"[本波 {report.waves}，共 {len(wave.items)} 项]"]
        for item in wave.items:
            lines.append(f"{item.id}. {item.content}")
            if item.verify:
                lines.append(f"   verify: {item.verify}")
        lines += [
            "",
            "按上面的顺序做；每完成一项就停下等验收；不要做本波之外的条目。",
            "完成后用 todo_write 标记 completed；做不了就标 blocked 并说明缺什么。",
            "本波收尾前调用一次 wave_report，交接口/决定/坑与下一波提示（没有可写就跳过）。",
        ]
        return "\n".join(lines)

    def _set_acceptance(self, session: SessionData, item: TodoItem) -> None:
        session.acceptance = (
            [AcceptanceItem(id=f"plan-{item.id}", statement=item.content, verify=item.verify)]
            if item.verify
            else []
        )

    def _set_policy(
        self, item: TodoItem, session: SessionData, *, continuations: int = 1
    ) -> None:
        if getattr(self._loop, "policy", None) is None or self._base_config is None:
            return
        base = self._base_config
        overrides: dict[str, Any] = {
            "max_iterations": max(1, self._setting_int("plan_step_max_iterations", 100)),
            "stop_policy": "natural",
            # One continuation, not zero: a step that ran out of iterations with
            # its item still open gets one more round.  It is safe only because
            # ``step_item_id`` narrows the open set to this item -- otherwise the
            # nudge would offer the whole rest of the plan to one step.
            #
            # The repair attempt (``continuations=0``) is the item's last
            # attempt.  C3 grants one continuation *per turn* (the counter
            # resets in ``_begin_turn``), so leaving it armed here would add
            # another 100 iterations on top of 100+100 (first attempt) + 100
            # (repair) -- 300 for one item instead of 200.
            "max_auto_continuations": max(0, int(continuations)),
        }
        # The scope the continuation reads (agent_loop._open_items_for).
        session.step_item_id = str(getattr(item, "id", "") or "")
        # A new item is a new piece of work: the previous item's ladder windows
        # (TASK refs, acceptance fingerprints) would otherwise read "helped"
        # against an item that is not this one.
        session.intervention_state = {}
        remaining = self._wave_remaining_tokens()
        if remaining is not None:
            overrides["max_turn_tokens"] = remaining
        self._loop.policy = TurnPolicy(replace(base, **overrides))

    def _arm_item_deadline(self, session: SessionData) -> None:
        """Give this item one wall clock, shared by every turn spent on it."""
        settings = getattr(self._loop, "settings", None)
        limit = float(getattr(settings, "plan_item_wall_seconds", 0.0) or 0.0)
        session.item_deadline = Deadline.after(limit) if limit > 0 else None

    def _item_passed(self, session: SessionData, item: TodoItem) -> bool:
        if item.verify:
            return self._acceptance_passed(session, item)
        current = self._task.get(item.id) if self._task is not None else None
        return current is not None and current.status == "completed"

    def _acceptance_passed(self, session: SessionData, item: TodoItem) -> bool:
        if not item.verify:
            return True
        return bool(session.acceptance) and all(
            entry.passed is True for entry in session.acceptance
        )

    def _acceptance_output(self, session: SessionData) -> str:
        for entry in getattr(session, "acceptance", ()):
            if entry.output:
                return str(entry.output)
        return ""

    # ---- report / receipts / persistence ---------------------------------

    def _record(
        self,
        report: PlanReport,
        parent: Any,
        delivery: StepDelivery,
        *,
        observer: Any = None,
        bubbles: bool = True,
    ) -> None:
        # Dedupe **within this run** only.  A previous run's receipt for the
        # same step id must not suppress a new attempt's completion receipt:
        # ``stalled``/``blocked`` receipts are re-attempted, and their id is
        # the item's id, not the attempt's.
        if delivery.step_id in self._recorded_this_run:
            return
        report.deliveries.append(delivery)
        parent.step_deliveries.append(delivery)
        self._recorded_this_run.add(delivery.step_id)
        if delivery.status == "completed":
            report.completed.append(delivery.step_id)
        elif delivery.status == "blocked":
            if delivery.step_id not in report.blocked:
                report.blocked.append(delivery.step_id)
        elif delivery.status == "skipped":
            if delivery.step_id not in report.skipped:
                report.skipped.append(delivery.step_id)
        elif delivery.status == "stalled":
            if delivery.step_id not in report.stalled:
                report.stalled.append(delivery.step_id)
        if bubbles:
            prompt = self._item_prompt_from_statement(delivery.statement)
            PlanRunner._record_on_parent(parent, prompt, delivery.summary, observer)
        elif observer is not None and hasattr(observer, "on_event"):
            try:
                observer.on_event(PlanStepReceipt(summary=delivery.summary))
            except Exception:  # noqa: BLE001 — observation must not cost the run
                pass

    def _delivery(
        self,
        item: TodoItem,
        status: str,
        attempts: int,
        passed: bool,
        summary: str,
        turns: tuple[int, int],
        usage: dict[str, int] | None,
        seed_chars: int,
        *,
        attested: bool = True,
    ) -> StepDelivery:
        return StepDelivery(
            step_id=item.id,
            statement=item.content,
            status=status,
            attempts=attempts,
            ac=[{"id": f"plan-{item.id}", "passed": passed}] if item.verify else [],
            summary=summary,
            turns=turns,
            usage=usage,
            attested=attested,
            seed_chars=seed_chars,
        )

    def _summary(self, item: TodoItem) -> str:
        first = _tail(self._last_text()).splitlines()
        return item.content if not first else f"{item.content}：{first[0]}"

    def _build_wave_note(
        self, session: SessionData, task: TaskState, wave: Wave
    ) -> str:
        done: list[str] = []
        skipped: list[str] = []
        for item in wave.items:
            current = task.get(item.id)
            if current is None:
                continue
            if current.status == "completed":
                done.append(item.id)
            elif current.status == "skipped":
                skipped.append(item.id)
        files = self._files_touched(session)
        parts: list[str] = []
        if done:
            parts.append("已完成：" + ",".join(done))
        if skipped:
            parts.append("跳过：" + ",".join(skipped))
        if files:
            parts.append("改动：" + ",".join(files))
        note = "；".join(parts)
        if len(note) > WAVE_NOTE_MAX_CHARS:
            note = note[: WAVE_NOTE_MAX_CHARS - 1] + "…"
        return note

    def _build_wave_handoff(
        self,
        session: SessionData,
        task: TaskState,
        wave: Wave,
        deliveries: list[StepDelivery],
    ) -> WaveHandoff:
        """Merge this wave's mechanical facts with the model's report (C3).

        ``done``/``skipped``/``files``/``verify`` come from the executor; the
        model's ``wave_report`` supplies only interfaces/decisions/pitfalls/
        hint (``WaveHandoff.build`` enforces that).
        """
        done: list[str] = []
        skipped: list[str] = []
        for item in wave.items:
            current = task.get(item.id)
            if current is None:
                continue
            if current.status == "completed":
                done.append(item.id)
            elif current.status == "skipped":
                skipped.append(item.id)
        verify: list[dict[str, Any]] = []
        for delivery in deliveries:
            entry = task.get(delivery.step_id)
            if entry is None or not entry.verify:
                continue
            passed = any(
                result.get("passed") is True for result in delivery.ac
            )
            verify.append(
                {
                    "itemId": delivery.step_id,
                    "command": entry.verify,
                    "passed": passed,
                    "tail": _tail(delivery.summary) if not passed else "",
                }
            )
        report = task.wave_report if isinstance(task.wave_report, dict) else None
        return WaveHandoff.build(
            done=tuple(done),
            skipped=tuple(skipped),
            files=tuple(self._files_touched(session)),
            verify=tuple(verify),
            report=report,
        )

    def _files_touched(self, session: SessionData) -> list[str]:
        seen: list[str] = []
        workspace = str(getattr(self._loop, "workspace", "") or "")
        for message in getattr(session, "messages", ()) or ():
            for artifact in getattr(message, "artifacts", ()) or ():
                path = str(getattr(artifact, "path", "") or "")
                if not path:
                    continue
                if workspace and path.startswith(workspace):
                    path = path[len(workspace):].lstrip("/")
                if path not in seen:
                    seen.append(path)
        return seen[:12]

    def _wave_signature(self, wave: Wave) -> str:
        return "w" + "-".join(wave.ids)

    def _current(self, task: TaskState, item: TodoItem) -> TodoItem | None:
        return task.get(item.id)

    def _next_ready(self, task: TaskState, wave: Wave) -> TodoItem | None:
        """First item of the wave this run may still work on.

        ``blocked`` is open (retryable next run) but not retryable inside the
        same run -- otherwise a failed item would be picked again immediately
        and spend the wave's budget on the same failure.
        """
        for item in wave.items:
            current = task.get(item.id)
            if current is None or current.is_terminal():
                continue
            if current.status == "blocked":
                continue
            if current.id in self._attempted_items:
                continue
            if task.blockers(current.id).any:
                continue
            return current
        return None

    # ---- wave-boundary replan (C3) ---------------------------------------

    async def _run_replan(
        self,
        parent: Any,
        task: TaskState,
        item: WaveItem,
        report: PlanReport,
        console: Any,
        *,
        on_delta: Any,
        observer: Any,
    ) -> None:
        """One ephemeral turn that may rewrite the remaining items.

        The model edits with ``todo_write`` only; a human must approve the new
        list before any work runs on it.  No change, no approval, or no prompt
        channel -> restore the snapshot and stop: this run never silently
        re-plans its own plan.
        """
        reason = item.replan
        report.replanned += 1
        snapshot = deepcopy(task.items)
        replan_session = self._make_replan_session(parent, task, reason)
        report.turns_used += 1
        self._say(console, f"[plan] {reason}；正在重规划剩余项…")
        try:
            await self._run_replan_turn(replan_session, task, reason, on_delta, observer)
        except asyncio.CancelledError:
            # The turn may already have rewritten the list through todo_write;
            # an unapproved rewrite must not survive the cancel.
            task.items = snapshot
            self._persist(parent)
            self._emit_task(task)
            raise
        except Exception as exc:  # noqa: BLE001 — a failed replan is a stop, not a crash
            task.items = snapshot
            report.stopped = f"replan failed: {exc}"
            self._persist(parent)
            self._emit_task(task)
            return
        usage = self._last_usage()
        if usage:
            report.tokens_used = self._merge_usage(report.tokens_used, usage)
        task.plan_tokens_used = (
            int(getattr(task, "plan_tokens_used", 0) or 0)
            + self._total_tokens(usage)
        )
        if not self._items_changed(snapshot, task.items):
            task.items = snapshot
            report.stopped = f"{reason}：重规划没有修改剩余项"
            self._persist(parent)
            self._emit_task(task)
            return
        if not await self._approved(task):
            task.items = snapshot
            report.stopped = "replan rejected：等待人工重新规划"
            self._persist(parent)
            self._emit_task(task)
            return
        self._persist(parent)
        self._emit_task(task)
        self._say(console, f"[plan] 已批准重规划（第 {report.replanned} 次）")

    async def _run_replan_turn(
        self,
        replan_session: SessionData,
        task: TaskState,
        reason: str,
        on_delta: Any,
        observer: Any,
    ) -> None:
        original = getattr(self._loop, "policy", None)
        base = self._base_config or getattr(original, "config", None)
        if original is not None and base is not None:
            self._loop.policy = TurnPolicy(
                replace(
                    base,
                    max_iterations=max(1, self._setting_int("plan_step_max_iterations", 100)),
                    stop_policy="natural",
                    max_auto_continuations=0,
                )
            )
        try:
            await self._loop.process_input(
                replan_session,
                self._replan_prompt(task, reason),
                on_delta=on_delta,
                observer=observer,
            )
        finally:
            if original is not None:
                self._loop.policy = original

    def _make_replan_session(
        self, parent: Any, task: TaskState, reason: str
    ) -> SessionData:
        session = SessionData(
            session_id=f"plan-replan-{uuid.uuid4().hex[:12]}",
            title="plan replan",
            created_time=now_utc(),
            last_access_time=now_utc(),
        )
        session.ephemeral = True
        session.task = task
        # Same rule as the wave session: the operator's grants must reach it.
        session.permissions = [dict(row) for row in getattr(parent, "permissions", ()) or ()]
        session.agent = "build"
        # C3: "only todo_write, never execute" is structural, not a prompt
        # request.  todo_write is non-mutating, so the read-only posture keeps
        # it (and read/grep/glob); write/edit/bash are dropped by the gate.
        session.readonly = True
        # The one fact ``todo_write`` reads: only open items may change here.
        session.replan_mode = True
        session.messages = [
            ChatMessage(
                role="user",
                content=self._replan_prompt(task, reason),
                timestamp="",
                synthetic=True,
            )
        ]
        return session

    @staticmethod
    def _replan_prompt(task: TaskState, reason: str) -> str:
        lines = [
            "[重规划]",
            f"原因：{reason}",
            f"目标：{task.goal or '(未命名计划)'}",
            "剩余未完成项：",
        ]
        open_items = task.open_items()
        if not open_items:
            lines.append("(没有剩余项)")
        for item in open_items:
            dep = f"（依赖 {','.join(item.depends_on)}）" if item.depends_on else ""
            lines.append(f"{item.id}. {item.content}{dep}")
        lines += [
            "",
            "只允许用 todo_write 调整上面这些未完成项：add 新项、update 现有项、"
            "或把某项标为 dropped。",
            "不要执行任何工作，不要改目标；已完成/已跳过/已删除的项不能改。",
            "如果无需调整，直接回答 NO_REPLAN。",
        ]
        return "\n".join(lines)

    async def _approved(self, task: TaskState) -> bool:
        """Ask the human to approve the rewritten list (C3, fail-closed)."""
        if self._user_prompt is None:
            return False
        ask = getattr(self._user_prompt, "ask", None)
        if ask is None:
            return False
        try:
            answer = await ask(
                "计划已重规划：批准新的剩余项并继续执行？",
                ("继续执行", "停止计划"),
                detail=task.render_display(),
                intent="plan_replan",
            )
        except Exception:  # noqa: BLE001 — no channel is a refusal, not a crash
            return False
        if not bool(getattr(answer, "answered", False)):
            return False
        return "继续执行" in str(getattr(answer, "text", "") or "")

    @staticmethod
    def _items_changed(before: list[TodoItem], after: list[TodoItem]) -> bool:
        def signature(items: list[TodoItem]) -> list[tuple[Any, ...]]:
            return [
                (
                    item.id,
                    item.content,
                    item.status,
                    tuple(item.depends_on),
                    item.verify,
                    tuple(item.files),
                    item.kind,
                )
                for item in items
            ]

        return signature(before) != signature(after)

    # ---- budgets / settings ----------------------------------------------

    def _setting_int(self, name: str, default: int) -> int:
        settings = getattr(self._loop, "settings", None)
        try:
            value = int(getattr(settings, name, default))
        except (TypeError, ValueError):
            return default
        return max(value, 0)

    def _attempts_cap(self) -> int:
        return max(1, self._setting_int("plan_max_item_attempts", 2))

    def _wave_budget(self) -> int:
        return self._setting_int("plan_wave_token_budget", 0)

    def _wave_budget_spent(self) -> bool:
        budget = self._wave_budget()
        return bool(budget and self._wave_tokens >= budget)

    def _wave_remaining_tokens(self) -> int | None:
        budget = self._wave_budget()
        if not budget:
            return None
        return max(1, budget - self._wave_tokens)

    def _episode_budget_spent(self, task: TaskState) -> bool:
        budget = self._setting_int("plan_token_budget", 0)
        spent = int(getattr(task, "plan_tokens_used", 0) or 0) + self._wave_tokens
        return bool(budget and spent >= budget)

    def _wave_time_spent(self, started: float | None) -> bool:
        limit = float(self._setting_int("plan_wave_wall_seconds", 0))
        if not limit or started is None:
            return False
        return (time.monotonic() - started) >= limit

    def _wave_budget_reason(self) -> str:
        budget = self._wave_budget()
        return (
            f"budget: wave token budget exhausted ({self._wave_tokens}/{budget}); "
            "remaining items stay open"
        )

    def _wave_time_reason(self) -> str:
        limit = float(self._setting_int("plan_wave_wall_seconds", 0))
        return (
            f"budget: wave wall-clock exhausted ({limit:.0f}s); remaining items stay open"
        )

    # ---- turn facts -------------------------------------------------------

    def _last_usage(self) -> dict[str, int] | None:
        outcome = getattr(self._loop, "last_outcome", None)
        usage = getattr(outcome, "usage", None)
        if not isinstance(usage, dict):
            return None
        try:
            return {str(key): int(value) for key, value in usage.items()}
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _total_tokens(usage: dict[str, int] | None) -> int:
        if not usage:
            return 0
        return max(int(usage.get("total_tokens", 0) or 0), 0)

    @staticmethod
    def _merge_usage(
        into: dict[str, int] | None, extra: dict[str, int] | None
    ) -> dict[str, int] | None:
        if into is None:
            return dict(extra) if extra is not None else None
        if extra is None:
            return into
        merged = dict(into)
        for key, value in extra.items():
            merged[key] = merged.get(key, 0) + int(value)
        return merged

    def _last_text(self) -> str:
        outcome = getattr(self._loop, "last_outcome", None)
        return str(getattr(outcome, "text", "") or "")

    @staticmethod
    def _tool_calls(session: SessionData) -> int:
        return sum(
            1
            for message in getattr(session, "messages", ()) or ()
            if getattr(message, "role", "") == "tool"
        )

    # ---- workspace gate / persistence / wire -----------------------------

    def _workspace_offenders(self, task: TaskState | None) -> list[str]:
        if task is None:
            return []
        workspace = str(getattr(self._loop, "workspace", "") or ".")
        extra_roots = tuple(getattr(self._loop, "extra_workspace_roots", ()) or ())
        items = task.open_items()
        offenders = outside_workspace_paths(
            [item.content for item in items], workspace, extra_roots=extra_roots
        )
        if offenders:
            return offenders
        return outside_workspace_paths(
            [item.verify for item in items if item.verify],
            workspace,
            extra_roots=extra_roots,
            from_shell=True,
        )

    def _persist(self, session: Any) -> None:
        save = getattr(getattr(self._loop, "closer", None), "save", None)
        if save is not None:
            save(session)

    def _emit_task(self, task: Any) -> None:
        """Say the task changed, on the event ``todo_write`` already uses."""
        if self._observer is None:
            return
        try:
            self._observer.on_event(TaskUpdated(task=task))
        except Exception:  # noqa: BLE001 — observation must not cost a wave
            pass

    def _item_prompt(self, item: TodoItem, *, repair: bool = False, failure: str = "") -> str:
        head = f"{WAVE_ITEM_HEAD}{item.content}\n"
        if repair and failure:
            return (
                f"{head}上一回合验收未通过，只修复失败点，不要重做已通过的部分：\n"
                f"{_tail(failure)}"
            )
        return f"{head}（计划条目 {item.id}；完成后停下等验收，不要做别的。）"

    def _item_prompt_from_statement(self, statement: str) -> str:
        return f"{WAVE_ITEM_HEAD}{statement}\n（计划的一项；完成后停下等验收。）"

    @staticmethod
    def _say(console: Any, text: str) -> None:
        if console is not None and text:
            console.info(text)
