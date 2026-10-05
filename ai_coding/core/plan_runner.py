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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from ai_coding.core.turn_policy import TurnPolicy
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.domain.session import SessionData
from ai_coding.infra.time_utils import now_utc

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


@contextmanager
def continuations_disabled(loop: Any) -> Iterator[None]:
    """Run a block with C3 (auto-continuation) off, then restore the policy.

    C3 pays off when the model is finishing *its own* work and stopped only
    because the iteration budget ran out.  It does not pay off when something
    else is waiting on the turn:

    - the planning turn, whose open items are the plan being authored rather
      than work in progress.  Continuing it told the model to "keep going" on
      the one turn whose prompt says *do not execute the work* (measured: 10
      model calls and 3 ``[continue]`` injections where 2 calls did the job);
    - plan steps, which get the same treatment inline in ``_run_step``;
    - sub-agents, which a parent already waits on and which carry their own
      wall-clock ceiling (``subagent._subagent_settings``).

    Plain ``loop.policy`` access on purpose: a loop without a policy is not an
    AgentLoop, and quietly skipping the swap would leave C3 armed in exactly
    the turn this exists to disarm.
    """
    original = loop.policy
    loop.policy = TurnPolicy(replace(original.config, max_auto_continuations=0))
    try:
        yield
    finally:
        loop.policy = original


@dataclass
class PlanReport:
    """What one ``/plan`` execution did, for the console and the caller."""

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
        head = f"[plan] 完成 {attested_done} 步 · 用了 {self.turns_used} 回合"
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
            # No task at all (a bare /plan on a session that never had one):
            # same verdict as an empty plan — "nothing to do", not "run /plan
            # first", which would be absurd right after planning.
            report.stopped = "no-open-items"
            self._say(console, "[plan] 完成 0 步 · 用了 0 回合（无任务项）")
            return report
        attempted: set[str] = set()
        cap = self._run_budget()
        # R1-5: ids that already have a receipt — this run's settlements plus
        # whatever the session still remembers about *this* list.  Anything
        # completed without one is a fiat completion the runner never settled:
        # receipt it, don't re-run it.
        #
        # Receipts are keyed by step id and ids restart at 1 for every list, so
        # a list replaced mid-turn (the model does todo_write(clear) and writes
        # a new one) leaves receipts whose ids collide with the new list's.  A
        # receipt describes the statement it settled, so anything whose
        # statement is no longer on the list belongs to a plan that is gone —
        # dropped here, before they can stand in for the new plan's steps.
        self._drop_foreign_receipts(session, task)
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
            # not an alarm.  Finished steps persist on the task; the remaining
            # open items survive as the plan's state.
            if 0 < cap <= report.turns_used:
                report.stopped = (
                    f"budget: plan turn budget exhausted "
                    f"({report.turns_used}/{cap} step-turns); "
                    "remaining steps stay open"
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
        self._persist(session)
        self._say(console, report.render())
        return report

    # ---- internals ------------------------------------------------------

    def _persist(self, session: Any) -> None:
        """Write the run's results back into the parent's session file.

        The steps run on ephemeral sessions (H1), so no turn inside the run
        saves the parent any more — before this, quitting right after
        ``/plan`` lost every receipt, every item status and every step bubble
        (measured: 0 receipts on disk after a completed run).  One save at the
        end of the run is the whole fix.  A later turn would have re-saved the
        same mutated object, which is exactly why the loss was easy to miss.
        """
        save = getattr(getattr(self._loop, "closer", None), "save", None)
        if save is not None:
            save(session)

    @staticmethod
    def _drop_foreign_receipts(session: Any, task: Any) -> None:
        """Forget receipts that describe steps this list does not contain.

        Step ids are per-list (they restart at 1), so once a list is replaced
        the old receipts are not history of the current plan — they are
        history of a plan that no longer exists, keyed identically.  Left in
        place they make the new plan's step 1 look already-receipted, and the
        run then skips backfilling a completion the model really made
        (measured: plan B's out-of-band step 1 produced no receipt at all).

        This destroys persisted history on purpose: the alternative keeps ids
        ambiguous forever.  ``/plan <topic>`` has always done the same thing
        through ``SessionData.begin_plan``; this closes the in-turn variant.
        """
        on_list = {item.content for item in task.items}
        kept = [entry for entry in getattr(session, "step_deliveries", [])
                if entry.statement in on_list]
        if len(kept) != len(session.step_deliveries):
            session.step_deliveries = kept

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

    def _make_step_session(self, task: Any, item: Any, report: PlanReport) -> SessionData:
        """A fresh session for one step — the short conversation (M1-B).

        Borrowed from the sub-agent runner (``core/subagent.py:_run_in``): a
        child gets its own ``SessionData`` rather than the parent's history.
        The seed is the structured handoff, so what a step reads is bounded by
        the plan instead of by the conversation — measured before: 4 turns
        cost 41k/82k/130k/254k prompt tokens, because each turn resent
        everything the earlier ones had said.
        """
        step = SessionData(
            session_id=f"plan-{uuid.uuid4().hex[:12]}",
            title=f"plan step {item.id}",
            created_time=now_utc(),
            last_access_time=now_utc(),
        )
        # Internal by definition (H1 audit fix): the step transcript is
        # disposable once its receipt is on the parent, and persisting it
        # would flood the sessions directory / /resume picker.
        step.ephemeral = True
        # Shared, not copied: a step marking its item done moves the plan.
        step.task = task
        step.messages = [
            ChatMessage(role="user", content=self._seed(task, item, report), timestamp="")
        ]
        return step

    def _seed(self, task: Any, item: Any, report: PlanReport) -> str:
        """The step's opening context: goal + the plan's current state.

        Only open items are listed — Hermes's rule that re-showing completed
        items makes the model redo finished work.

        What this buys, precisely: a step's context is bounded by *the plan*,
        not by the conversation.  Before, every step resent the parent's whole
        history, so turn k carried everything steps 1..k-1 said — 4 turns cost
        41k/82k/130k/254k prompt tokens.  Now nothing a step said can reach a
        later step.

        What it does not buy: the seed re-lists the open items, so the total
        seed text over an N-step plan is still quadratic in N (measured: 8
        steps 2.4k chars, 16 → 8.8k, 32 → 34k, 64 → 132k) — with a constant
        roughly two orders of magnitude smaller than transcripts.  Calling
        that "linear" was wrong; the per-step size is what stayed flat.
        """
        lines = [f"[计划目标] {task.goal or '(未命名计划)'}", "", "[计划步骤]"]
        for entry in task.items:
            if entry.is_terminal():
                continue
            marker = " ← 本步" if entry.id == item.id else ""
            lines.append(f"{entry.id}. [{entry.status}] {entry.content}{marker}")
        if item.verify:
            lines += ["", f"[本步验收] {item.verify}"]
        return "\n".join(lines)

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
            f"[plan] 步骤 {item.id}/{total}（剩余 {open_count} 项）：{item.content}",
        )
        # M1-B: the step runs in its own short conversation, not the parent's
        # ever-growing history.  The parent keeps only the prompt (replay
        # marker) and a one-line summary.
        step_session = self._make_step_session(task, item, report)
        seed = step_session.messages[0].content or ""
        contracts = (
            [AcceptanceItem(
                id=f"plan-{item.id}", statement=item.content, verify=item.verify,
            )]
            if item.verify else []
        )
        original_policy = self._loop.policy
        budget = self._step_budget()
        # M1-B: close the todo gate too — a step must not be nudged about the
        # plan's other open items.  Same swap-and-restore discipline as before.
        # C3: 0 as well — the step session shares the plan's task, so on budget
        # exhaustion every remaining plan item would look like "unfinished
        # work" and one step would spend the whole plan's list.  A step is one
        # bounded turn, by design.
        self._loop.policy = TurnPolicy(
            replace(original_policy.config, max_iterations=budget,
                    stop_policy="natural", max_auto_continuations=0)
        )
        attempts = 0
        step_usage: dict[str, int] | None = None
        # Built before the try: a step that dies mid-turn still has to leave a
        # replay marker on the parent, and the marker is this prompt.
        step_prompt = self._step_prompt(item)
        try:
            step_session.acceptance = list(contracts)
            start_turn = int(getattr(step_session, "turn_count", 0) or 0)
            text = await self._loop.process_input(
                step_session, step_prompt,
                on_delta=on_delta, observer=observer,
            )
            attempts = 1
            # R1-4: every returned turn costs budget, and its spend is read
            # off the loop while it is still "the last turn".
            report.turns_used += 1
            turn_usage = self._turn_usage()
            step_usage = self._merge_usage(step_usage, turn_usage)
            report.tokens_used = self._merge_usage(report.tokens_used, turn_usage)
            if contracts and not self._contracts_passed(step_session):
                text = await self._loop.process_input(
                    step_session, self._repair_prompt(item, step_session),
                    on_delta=on_delta, observer=observer,
                )
                attempts = 2
                report.turns_used += 1
                turn_usage = self._turn_usage()
                step_usage = self._merge_usage(step_usage, turn_usage)
                report.tokens_used = self._merge_usage(report.tokens_used, turn_usage)
            end_turn = int(getattr(step_session, "turn_count", 0) or 0)
            delivery = self._settle(step_session, session, task, item, report, text,
                                   attempts, (start_turn, end_turn), step_usage,
                                   len(seed))
            self._record_on_parent(session, step_prompt, delivery.summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad step must not kill the run
            task.update(item.id, status="blocked",
                        evidence=f"runner error: {exc}")
            report.blocked.append(item.id)
            delivery = self._delivery(
                item, "blocked", 1, [], f"runner error: {exc}", (0, 0),
                step_usage, len(seed))
            # Both halves of the parent's record, exactly as the settled path
            # leaves them: the receipt in the book and the bubble for replay.
            # This path used to write neither, so a step that crashed the turn
            # was blocked in the task list and invisible everywhere else — no
            # receipt to match a header against, no bubble to match at all.
            report.deliveries.append(delivery)
            session.step_deliveries.append(delivery)
            self._record_on_parent(session, step_prompt, delivery.summary)
        finally:
            self._loop.policy = original_policy

    @staticmethod
    def _record_on_parent(session: Any, prompt: str, summary: str) -> None:
        """Leave the parent's two-bubble trace of one step.

        The prompt is the replay marker (``STEP_HEAD``); the summary is the
        one line the user reads instead of the step's tools.  The step's own
        transcript stays in its short conversation.  Both the settled and the
        failed path go through here, because a receipt with no bubble is a
        step ``/resume`` cannot show.

        Both bubbles are stamped like any other persisted message: these are
        replayed through the same view as the rest of the transcript, and an
        unstamped one shows an empty clock (``_hhmm`` passes non-ISO through)
        and exports as epoch zero.  ``timestamp`` is display-only — nothing
        sorts or trims by it.
        """
        now = now_utc()
        session.add_message(ChatMessage(role="user", content=prompt, timestamp=now))
        session.add_message(ChatMessage(role="assistant", content=summary, timestamp=now))

    def _contracts_passed(self, step_session: Any) -> bool:
        return bool(step_session.acceptance) and all(
            item.passed is True for item in step_session.acceptance
        )

    def _settle(self, step_session: Any, parent: Any, task: Any, item: Any, report: PlanReport,
                text: Any, attempts: int, turns: tuple[int, int],
                usage: dict[str, int] | None, seed_chars: int) -> StepDelivery:
        """Score one finished step, record its delivery, and return it.

        Mechanical verdicts win: a step whose proof commands fail is blocked
        even if the model marked it done; a step without proof commands that
        the model left open is stalled (retryable next run), never retried
        inside this run.

        ``step_session`` is where the turn ran (its acceptance verdicts live);
        ``parent`` is the session that keeps the receipt — the step's own
        transcript stays behind in the short conversation.
        """
        ac = [
            {"id": entry.id, "passed": entry.passed}
            for entry in getattr(step_session, "acceptance", [])
        ]
        first_line = _tail(str(text)).splitlines()
        summary = item.content if not first_line else f"{item.content}：{first_line[0]}"
        status = "completed"
        if item.verify:
            if self._contracts_passed(step_session):
                task.update(item.id, status="completed",
                            evidence=f"verify passed: {_tail(self._verify_output(step_session))}")
            else:
                task.update(item.id, status="blocked",
                            evidence=f"verify failed: {_tail(self._verify_output(step_session))}")
                status = "blocked"
                report.blocked.append(item.id)
        elif not self._model_claimed_done(task, item):
            status = "stalled"
            report.stalled.append(item.id)
        if status == "completed":
            report.completed.append(item.id)
        delivery = self._delivery(item, status, attempts, ac, summary, turns, usage, seed_chars)
        report.deliveries.append(delivery)
        parent.step_deliveries.append(delivery)
        return delivery

    def _model_claimed_done(self, task: Any, item: Any) -> bool:
        current = task.get(item.id)
        return current is not None and current.status == "completed"

    def _verify_output(self, step_session: Any) -> str:
        for entry in getattr(step_session, "acceptance", []):
            if entry.output:
                return str(entry.output)
        return ""

    def _delivery(self, item: Any, status: str, attempts: int,
                ac: list[dict[str, Any]], summary: str,
                turns: tuple[int, int], usage: dict[str, int] | None = None,
                seed_chars: int = 0) -> StepDelivery:
        return StepDelivery(
            step_id=item.id, statement=item.content, status=status,
            attempts=attempts, ac=ac, summary=summary, turns=turns,
            usage=usage, seed_chars=seed_chars,
        )

    def _step_prompt(self, item: Any) -> str:
        return (
            f"{STEP_HEAD}{item.content}\n"
            f"（这是计划步骤 {item.id}，做完停下等验收，不要展开做别的。）"
        )

    def _repair_prompt(self, item: Any, step_session: Any) -> str:
        return (
            f"{STEP_HEAD}{item.content}\n"
            f"上一步验收失败，只修复失败点，不要重做已通过的部分：\n"
            f"{self._verify_output(step_session)}"
        )

    @staticmethod
    def _say(console: Any, text: str) -> None:
        if console is not None and text:
            console.info(text)


__all__ = ["PlanReport", "PlanRunner", "STEP_HEAD"]
