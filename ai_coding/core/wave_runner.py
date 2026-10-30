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
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import WAVE_NOTE_MAX_CHARS, TaskState, TodoItem
from ai_coding.infra.time_utils import now_utc

__all__ = ["WaveRunner", "WaveItem", "WAVE_ITEM_HEAD", "plan_runner_for"]


def plan_runner_for(loop: Any) -> Any:
    """The executor the configuration asks for (``wave`` unless told otherwise).

    One selection point for the command layer and the server: the legacy runner
    stays reachable as ``plan_orchestrator: legacy`` and keeps every one of its
    tests, while the default moves to waves.
    """
    settings = getattr(loop, "settings", None)
    name = str(getattr(settings, "plan_orchestrator", "wave") or "wave")
    if name == "legacy":
        from ai_coding.core.plan_runner import PlanRunner

        return PlanRunner(loop)
    return WaveRunner(loop)


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
    """One wave as the driver's "item" — it needs an id and an order."""

    id: str
    wave: Wave


class WaveRunner:
    """The ``plan`` RoundProfile that executes a plan wave by wave."""

    def __init__(self, loop: Any) -> None:
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
        return self._receipted

    def next_item(self, task: Any, attempted: set[str]) -> WaveItem | None:
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
        return None

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
        await self._run_wave(
            session, task, item.wave, report, console,
            on_delta=on_delta, observer=observer,
        )

    def after_round(self, session: Any, task: Any, report: PlanReport) -> None:
        task.rounds_run = int(getattr(task, "rounds_run", 0) or 0) + 1
        self._persist(session)

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
        seed = self._seed(task, wave, report)
        wave_session = self._make_wave_session(parent, task, seed, report)
        started = time.monotonic()
        self._wave_started = started
        original_policy = self._loop.policy
        self._base_config = original_policy.config
        attempts_cap = self._attempts_cap()
        self._say(console, f"[plan] 第 {report.waves} 波（{len(wave.items)} 项）")
        try:
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
                _moved, usage = await self._run_item(
                    wave_session, parent, task, wave, item, report,
                    console, on_delta=on_delta, observer=observer,
                    attempts_cap=attempts_cap,
                )
                if usage:
                    report.tokens_used = self._merge_usage(report.tokens_used, usage)
                # The model may have finished more than the item it was asked
                # about; settle them before choosing the next one.
                await self._settle_extra_completions(
                    wave_session, parent, task, wave, report, observer,
                )
        finally:
            self._loop.policy = original_policy

        task.wave_note = self._build_wave_note(wave_session, task, wave)
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
            self._set_policy(item, wave_session)
            self._set_acceptance(wave_session, item)
            prompt = self._item_prompt(item, repair=attempts > 1, failure=failure)
            try:
                await self._loop.process_input(
                    wave_session, prompt, on_delta=on_delta, observer=observer
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
                wave_session, prompt, on_delta=on_delta, observer=observer
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
        """Settle items the model finished while working on another one."""
        for item in wave.items:
            current = task.get(item.id)
            if current is None or current.status != "completed":
                continue
            if current.id in self._recorded_this_run:
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
                bool(self._acceptance_passed(wave_session, current)),
                self._summary(current),
                (0, int(getattr(wave_session, "turn_count", 0) or 0)),
                self._last_usage(),
                len(wave_session.messages),
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
            ChatMessage(role="user", content=seed, timestamp="")
        ]
        return session

    def _seed(self, task: TaskState, wave: Wave, report: PlanReport) -> str:
        lines = [f"[计划标题] {task.goal or '(未命名计划)'}"]
        if task.wave_note:
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
        ]
        return "\n".join(lines)

    def _set_acceptance(self, session: SessionData, item: TodoItem) -> None:
        session.acceptance = (
            [AcceptanceItem(id=f"plan-{item.id}", statement=item.content, verify=item.verify)]
            if item.verify
            else []
        )

    def _set_policy(self, item: TodoItem, session: SessionData) -> None:
        if getattr(self._loop, "policy", None) is None or self._base_config is None:
            return
        base = self._base_config
        overrides: dict[str, Any] = {
            "max_iterations": max(1, self._setting_int("plan_step_max_iterations", 60)),
            "stop_policy": "natural",
            "max_auto_continuations": 0,
        }
        remaining = self._wave_remaining_tokens()
        if remaining is not None:
            overrides["max_turn_tokens"] = remaining
        self._loop.policy = TurnPolicy(replace(base, **overrides))

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
            attested=True,
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
