"""Plan runner (R1-1): a todo plan becomes executable budget allocations.

A plan is a todo list; a run executes its open items in dependency order, one
bounded turn each, with proof commands verified through the R0 gate that
already lives in the loop.  Each step leaves a delivery receipt on the
session; the resume replay rebuilds the step timeline from those receipts.

Design notes (all measured in the 23-run experiment batch):

* One bounded turn per step, not one giant turn: chained short runs used
  ~30% fewer tokens than one-shot runs on the same work, because a scoped
  prompt explores less redundantly.
* The loop's own policy is swapped for a bounded copy per step and restored
  in a ``finally`` — the runner never mutates shared orchestration state.
* Mechanical verdicts win over model claims: a step whose proof commands
  fail is ``blocked`` even if the model marked it done; a step the model
  left open without proof commands is ``stalled`` (retryable next run),
  never silently retried inside one run.
* One repair run per failing step, then stop.  Re-planning is the user's
  explicit decision (``/plan`` again), never an automatic loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

from ai_coding.core.turn_policy import TurnPolicy
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.plan_delivery import StepDelivery

#: Step prompts start here.  The replay matches user bubbles on this marker
#: to rebuild step headers — turn numbers do not survive compaction, but the
#: prompt text travels inside the persisted user message.
STEP_HEAD = "只做这一步，不要做别的："

#: Cap on the failure output quoted into a repair prompt (same 500-char
#: preview discipline as the acceptance evidence tails).
_OUTPUT_TAIL = 500


def _tail(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _OUTPUT_TAIL else f"…{text[-_OUTPUT_TAIL:]}"


@dataclass
class PlanReport:
    """What one ``/plan-run`` did, for the console and the caller."""

    completed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    deliveries: list[StepDelivery] = field(default_factory=list)
    #: None = ran to the end; otherwise why it stopped early.
    stopped: str | None = None
    #: Step-turns (``process_input`` calls) this run spent (R1-4).
    turns_used: int = 0
    #: Summed model-step usage, or None when the loop reported none (R1-4).
    tokens_used: dict[str, int] | None = None

    def render(self) -> str:
        # R1-5b: the headline counts work this run did (attested).  Fiat
        # receipts are real completions but not this run's work — claiming
        # them in the headline read as a lie (measured: "完成 4 步" for 0
        # turns).  report.completed still lists every done id (API stable);
        # only the human sentence splits the provenance.
        attested_of = {entry.step_id: entry.attested for entry in self.deliveries}
        attested_done = sum(
            1 for step_id in self.completed if attested_of.get(step_id, True)
        )
        head = f"[plan-run] 完成 {attested_done} 步 · 用了 {self.turns_used} 回合"
        if self.tokens_used:
            head += f" / {self.tokens_used.get('total_tokens', '?')} tokens"
        parts = [head]
        unattested = sum(1 for entry in self.deliveries if not entry.attested)
        if unattested:
            parts.append(f"(含 {unattested} 步分步外完成，未经分步验收)")
        on_claim = [entry.step_id for entry in self.deliveries if entry.proof == "on-claim"]
        if on_claim:
            parts.append(f"(含 {len(on_claim)} 步无证明完成：{', '.join(on_claim)})")
        if self.completed:
            parts.append("已完成：" + ", ".join(self.completed))
        if self.blocked:
            parts.append("受阻：" + ", ".join(self.blocked))
        if self.stalled:
            parts.append("停滞（下次可重跑）：" + ", ".join(self.stalled))
        if self.stopped:
            parts.append(f"止于：{self.stopped}")
        return "\n".join(parts)


class PlanRunner:
    """Execute a session's todo plan step by step through a loop."""

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    async def run(
        self,
        session: Any,
        console: Any = None,
        *,
        on_delta: Any = None,
        observer: Any = None,
    ) -> PlanReport:
        """Run every ready todo item; return what happened."""
        report = PlanReport()
        task = getattr(session, "task", None)
        if task is None:
            report.stopped = "no-open-items"
            self._say(console, "[plan-run] no open items — run /plan first")
            return report
        attempted: set[str] = set()
        cap = self._run_budget()
        # R1-5: ids that already have a receipt — history (resumed runs) plus
        # this run's settlements.  Anything completed without one is a fiat
        # completion the runner never settled: receipt it, don't re-run it.
        receipted: set[str] = {
            entry.step_id for entry in getattr(session, "step_deliveries", [])
        }
        while True:
            self._backfill_fiat(task, session, report, receipted)
            item = self._next_ready(task, attempted)
            if item is None:
                if task.open_items():
                    report.stopped = (
                        "deadlock: open items remain but none is ready "
                        "(missing dependency or cycle)"
                    )
                break
            # R1-4: the run's own ceiling, checked only while work remains —
            # a run that spends exactly the cap finishing is a clean end,
            # not an alarm.  Finished steps persist on the task, so the next
            # /plan-run resumes where this one stopped.
            if 0 < cap <= report.turns_used:
                report.stopped = (
                    f"budget: plan turn budget exhausted "
                    f"({report.turns_used}/{cap} step-turns); "
                    "re-run /plan-run to continue"
                )
                break
            attempted.add(item.id)
            await self._run_step(session, task, item, report, console,
                                 on_delta=on_delta, observer=observer)
            receipted.add(item.id)  # settled (or runner-blocked) above
            if report.stalled:
                break
            if report.blocked and report.blocked[-1] == item.id:
                break
        self._say(console, report.render())
        return report

    # ---- internals ------------------------------------------------------

    def _next_ready(self, task: Any, attempted: set[str]) -> Any:
        """First open item with no blockers that this run has not tried."""
        for item in task.open_items():
            if item.is_terminal() or item.id in attempted:
                continue
            if task.blockers(item.id).any:
                continue
            return item
        return None

    def _backfill_fiat(
        self, task: Any, session: Any, report: PlanReport,
        receipted: set[str],
    ) -> None:
        """Receipt steps the model finished outside any step turn (R1-5).

        Pure observation: the task state is already right (done is done), so
        nothing is re-run, verified, or mutated — the run just writes down
        what happened and where it did not vouch.  Zero turn cost: fiat
        completions never touch the R1-4 budget.  Runs at the loop top, so
        even a run that stops right after (stalled/blocked/budget) leaves
        the receipts behind.
        """
        for item in task.items:
            if item.id in receipted or item.status != "completed":
                continue
            delivery = StepDelivery(
                step_id=item.id,
                statement=item.content,
                status="completed",
                attempts=0,
                ac=[],
                summary=(
                    f"{item.content}：模型在本轮分步之外标 done，"
                    "未经有界回合与验收门，原文照收"
                ),
                turns=(0, 0),
                usage=None,
                attested=False,
            )
            report.deliveries.append(delivery)
            session.step_deliveries.append(delivery)
            receipted.add(item.id)
            report.completed.append(item.id)

    def _step_budget(self) -> int:
        settings = getattr(self._loop, "settings", None)
        return int(getattr(settings, "plan_step_max_iterations", 8) or 8)

    def _run_budget(self) -> int:
        """Turn ceiling for this run (R1-4): 0 (or negative) disables."""
        settings = getattr(self._loop, "settings", None)
        value = getattr(settings, "plan_max_step_turns", 20)
        if value is None:
            return 20
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 20

    @staticmethod
    def _merge_usage(
        into: dict[str, int] | None, extra: dict[str, int] | None
    ) -> dict[str, int] | None:
        """Sum two usage dicts key-wise; None when neither reported."""
        if into is None:
            return dict(extra) if extra is not None else None
        if extra is None:
            return into
        merged = dict(into)
        for key, value in extra.items():
            merged[key] = merged.get(key, 0) + int(value)
        return merged

    def _turn_usage(self) -> dict[str, int] | None:
        """What the last ``process_input`` spent, if the loop says (R1-4).

        The real loop exposes ``last_outcome.usage``; stubs do not — the
        getattr chain keeps both working, and unknown stays unknown (None),
        never zero (zero would lie about a free turn).
        """
        outcome = getattr(self._loop, "last_outcome", None)
        usage = getattr(outcome, "usage", None)
        if not isinstance(usage, dict):
            return None
        try:
            return {str(key): int(value) for key, value in usage.items()}
        except (TypeError, ValueError):
            return None

    async def _run_step(
        self, session: Any, task: Any, item: Any, report: PlanReport,
        console: Any, *, on_delta: Any, observer: Any,
    ) -> None:
        open_count = len(task.open_items())
        total = len(task.items)
        self._say(
            console,
            f"[plan-run] 步骤 {item.id}/{total}（剩余 {open_count} 项）：{item.content}",
        )
        previous_acceptance = list(getattr(session, "acceptance", []))
        contracts = (
            [AcceptanceItem(
                id=f"plan-{item.id}", statement=item.content, verify=item.verify,
            )]
            if item.verify else []
        )
        original_policy = self._loop.policy
        budget = self._step_budget()
        self._loop.policy = TurnPolicy(
            replace(original_policy.config, max_iterations=budget)
        )
        context = self._context(report)
        attempts = 0
        step_usage: dict[str, int] | None = None
        try:
            session.acceptance = list(contracts)
            start_turn = int(getattr(session, "turn_count", 0) or 0)
            text = await self._loop.process_input(
                session, self._step_prompt(item, context),
                on_delta=on_delta, observer=observer,
            )
            attempts = 1
            # R1-4: every returned turn costs budget, and its spend is read
            # off the loop while it is still "the last turn".
            report.turns_used += 1
            turn_usage = self._turn_usage()
            step_usage = self._merge_usage(step_usage, turn_usage)
            report.tokens_used = self._merge_usage(report.tokens_used, turn_usage)
            if contracts and not self._contracts_passed(session):
                text = await self._loop.process_input(
                    session, self._repair_prompt(item, session),
                    on_delta=on_delta, observer=observer,
                )
                attempts = 2
                report.turns_used += 1
                turn_usage = self._turn_usage()
                step_usage = self._merge_usage(step_usage, turn_usage)
                report.tokens_used = self._merge_usage(report.tokens_used, turn_usage)
            end_turn = int(getattr(session, "turn_count", 0) or 0)
            self._settle(session, task, item, report, text, attempts,
                         (start_turn, end_turn), step_usage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad step must not kill the run
            task.update(item.id, status="blocked",
                        evidence=f"runner error: {exc}")
            report.blocked.append(item.id)
            report.deliveries.append(self._delivery(
                item, "blocked", 1, [], f"runner error: {exc}", (0, 0),
                step_usage))
        finally:
            self._loop.policy = original_policy
            session.acceptance = previous_acceptance

    def _contracts_passed(self, session: Any) -> bool:
        return bool(session.acceptance) and all(
            item.passed is True for item in session.acceptance
        )

    def _settle(self, session: Any, task: Any, item: Any, report: PlanReport,
                text: Any, attempts: int, turns: tuple[int, int],
                usage: dict[str, int] | None) -> None:
        """Score one finished step and record its delivery.

        Mechanical verdicts win: a step whose proof commands fail is blocked
        even if the model marked it done; a step without proof commands that
        the model left open is stalled (retryable next run), never retried
        inside this run.
        """
        ac = [
            {"id": entry.id, "passed": entry.passed}
            for entry in getattr(session, "acceptance", [])
        ]
        first_line = _tail(str(text)).splitlines()
        summary = item.content if not first_line else f"{item.content}：{first_line[0]}"
        status = "completed"
        if item.verify:
            if self._contracts_passed(session):
                task.update(item.id, status="completed",
                            evidence=f"verify passed: {_tail(self._verify_output(session))}")
            else:
                task.update(item.id, status="blocked",
                            evidence=f"verify failed: {_tail(self._verify_output(session))}")
                status = "blocked"
                report.blocked.append(item.id)
        elif not self._model_claimed_done(task, item):
            status = "stalled"
            report.stalled.append(item.id)
        if status == "completed":
            report.completed.append(item.id)
        delivery = self._delivery(item, status, attempts, ac, summary, turns, usage)
        report.deliveries.append(delivery)
        session.step_deliveries.append(delivery)

    def _model_claimed_done(self, task: Any, item: Any) -> bool:
        current = task.get(item.id)
        return current is not None and current.status == "completed"

    def _verify_output(self, session: Any) -> str:
        for entry in getattr(session, "acceptance", []):
            if entry.output:
                return str(entry.output)
        return ""

    def _delivery(self, item: Any, status: str, attempts: int,
                ac: list[dict[str, Any]], summary: str,
                turns: tuple[int, int], usage: dict[str, int] | None = None) -> StepDelivery:
        return StepDelivery(
            step_id=item.id, statement=item.content, status=status,
            attempts=attempts, ac=ac, summary=summary, turns=turns,
            usage=usage,
        )

    def _context(self, report: PlanReport) -> str:
        if not report.deliveries:
            return ""
        lines = ["已完成步骤："]
        lines.extend(
            f"{entry.step_id}. {entry.statement}（{entry.status}）"
            for entry in report.deliveries
        )
        return "\n".join(lines)

    def _step_prompt(self, item: Any, context: str) -> str:
        prompt = (
            f"{STEP_HEAD}{item.content}\n"
            f"（这是计划步骤 {item.id}，做完停下等验收，不要展开做别的。）"
        )
        if context:
            prompt += f"\n{context}"
        return prompt

    def _repair_prompt(self, item: Any, session: Any) -> str:
        return (
            f"{STEP_HEAD}{item.content}\n"
            f"上一步验收失败，只修复失败点，不要重做已通过的部分：\n"
            f"{self._verify_output(session)}"
        )

    @staticmethod
    def _say(console: Any, text: str) -> None:
        if console is not None and text:
            console.info(text)


__all__ = ["PlanReport", "PlanRunner", "STEP_HEAD"]
