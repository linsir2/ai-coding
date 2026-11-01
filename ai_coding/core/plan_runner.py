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
from dataclasses import replace
from typing import Any

from ai_coding.core.events import PlanStepReceipt, TaskUpdated
from ai_coding.core.plan_report import PlanReport
from ai_coding.core.task_driver import TaskDriver
from ai_coding.core.turn_policy import TurnPolicy

# P0b: 本文件删除时一并删除这些转发。
from ai_coding.core.workspace_guard import (
    outside_workspace_message,
    outside_workspace_paths,
)
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.agents import PLAN_DIR
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.domain.run import user_message
from ai_coding.domain.session import SessionData
from ai_coding.infra.deadline import Deadline
from ai_coding.infra.time_utils import now_utc

#: Step prompts start here.  The replay matches user bubbles on this marker
#: to rebuild step headers — turn numbers do not survive compaction, but the
#: prompt text travels inside the persisted user message.
#: The mark on the harness's own line for a planning round.  The frontend
#: skips a user message carrying it (see ``state/replay.ts``), so the
#: transcript shows the human's words and not the instruction.  It is the
#: same convention the other harness lines use (``[notice] ``, ``[steering] ``).
PLAN_MARK = "[plan] "

STEP_HEAD = "只做这一步，不要做别的："

#: The planning round's exploration budget: how many rounds the model may
#: spend before it has to hand something over.
#:
#: It used to share ``max_tool_iterations`` and died mid-plan (measured
#: twice: 15 iterations eaten by large reads, a half-written plan reported as
#: ready).  It then moved to 16, which a real repository plan (23 steps of a
#: hotel system) can still exceed -- and a planning turn that ends on its
#: budget no longer auto-runs its approved plan.  32 clears the measured
#: shape (4-6 rounds for a small plan, 16-25 for a large one) while still
#: bounding an exploration that never submits.
PLANNING_MAX_ITERATIONS = 32

#: Cap on the failure output quoted into a repair prompt (same 500-char
#: preview discipline as the acceptance evidence tails).
_OUTPUT_TAIL = 500


def _tail(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _OUTPUT_TAIL else f"…{text[-_OUTPUT_TAIL:]}"


#: The planning prompt.  One definition: it used to be written out twice (the
#: Textual front-end and the REPL dispatcher), which is how the two drifted.
#:
#: The discriminator comes first: the request decides the exit, and only then
#: do the two exits get described.  The old prompt opened with "Create a
#: detailed implementation plan for: {topic}" -- an instruction that commands
#: a plan even for a request that only asks to be surveyed -- so a research
#: request came back with a plan, and that plan hit the workspace gate
#: (measured, production session 135cc71c: 19 rounds, then a retry loop and
#: invented text).  The rule itself is the same one the ``finish_research``
#: tool teaches, so the model does not learn two criteria.
PLAN_PROMPT = (
    "Decide first which exit this topic wants: a request that asks for "
    "changes (implement, modify, add, fix, refactor) needs a plan, and a "
    "request that needs no implementation (a survey, an analysis, a "
    "comparison) needs a conclusion. "
    "A request that needs a plan gets one like this. "
    "Create a detailed implementation plan for: {topic}. "
    "Break it down into specific, actionable steps. "
    "Submit the plan with present_plan: a concise markdown summary (a title "
    "first) and, in its items array, the executable work items. One item is "
    "one deliverable outcome, not one file read: merge reading and analysis "
    "into the work that needs it, and keep an implementation plan to at most "
    "six items. Put a shell command in 'verify' only when a real acceptance "
    "command exists; never use a file the plan itself produces (for example "
    "under the plan directory) as its own proof. "
    "Write the items as pending/open steps only — do not execute "
    "the work and do not mark items completed during planning. "
    "State each step's goal and its verifiable success criteria, "
    "and make the plan decision-complete: another engineer should be "
    "able to implement it without making further design decisions. "
    "Write the full skeleton of items first (one line each, plus its verify "
    "only where there is a real command) and refine afterwards; use "
    "grep/glob to look at code selectively "
    "— do not read whole large files just to write the plan. "
    f"Write only inside the plan directory ({PLAN_DIR}/); bash is "
    "unavailable in plan mode. Explore with read/grep/glob; for read-only shell "
    "inspection (git log/status/diff, find, wc, ls) use shell_read -- one simple "
    "command, no pipes or redirection. For 2-4 "
    "independent read-only questions use parallel_research, which runs them "
    "concurrently in read-only sub-agents and returns one merged report. "
    "Do not hand subAgent absolute paths of this repository: it runs in an "
    "isolated worktree and must be given paths relative to it. "
    "Temp files, test data and output directories must live inside the workspace; "
    "do not use /tmp. "
    "present_plan presents the plan for the user's review, and approval "
    "starts execution automatically — do not execute it yourself. "
    "A topic that needs no implementation closes with finish_research(summary) "
    "instead of a plan; a research or analysis request produces one report, "
    "not a plan whose items each write a separate document. "
    "Imperative language to implement ('go ahead', 'do it') means plan the "
    "implementation, not execute it. A conversational agreement approves "
    "nothing: only present_plan's approval starts execution. Ask the user "
    "only for choices you cannot discover by inspection."
)


def _persist_session(loop: Any, session: Any) -> None:
    """Write the session back, if this loop has a way to do it.

    ``plan_only`` is a module-level function, so it cannot use
    ``PlanRunner._persist``; the shape is the same one that method uses
    (``loop.closer.save``), reached defensively because a test's stub loop
    may not carry a closer at all.
    """
    save = getattr(getattr(loop, "closer", None), "save", None)
    if save is not None:
        save(session)


def _emit_task(observer: Any, task: Any) -> None:
    """R06: the task's own mutations travel the wire like the model's tool
    calls do, so a live card sees the plan withdrawn rather than freezing on
    a plan that was refused.  None-safe: a bare caller stays silent."""
    if observer is not None and hasattr(observer, "on_event"):
        observer.on_event(TaskUpdated(task=task))


async def plan_only(
    loop: Any,
    session: Any,
    *,
    topic: str,
    on_delta: Any = None,
    observer: Any = None,
    images: tuple[Any, ...] = (),
) -> PlanReport:
    """``/plan <topic>``: plan the topic and stop — the planning half.

    The old one-shot ``plan_and_run`` is gone (18 §10.1): the command layer
    composes the halves explicitly (entry/exit notices and ``/plan run`` live
    in ``app/commands.py``).  ``topic`` is required — a bare ``/plan`` only
    flips the posture, so there is nothing to ask and no asking path to keep.

    Two workspace gates kept from the old workflow: #1 refuses a target
    outside the workspace before any model call, #2 refuses a written plan
    that names unreachable paths (the caller shows ``report.stopped``).
    """
    workspace = str(getattr(loop, "workspace", "") or ".")
    extra_roots = tuple(getattr(loop, "extra_workspace_roots", ()) or ())
    offenders = outside_workspace_paths([topic], workspace, extra_roots=extra_roots)
    if offenders:
        return PlanReport(stopped=outside_workspace_message(offenders))
    # An armed plan is a pending commitment: the human approved it and the
    # runner has not executed it yet.  A new planning round would swap the
    # whole book (``begin_plan``) and silently un-approve it -- the plan
    # would never run, and nobody would have said so.  Refuse instead, in
    # the same shape the workspace gate uses.
    task = getattr(session, "task", None)
    if task is not None and getattr(task, "armed", False):
        return PlanReport(
            stopped=(
                "已有一份批准的计划尚未执行；先执行它（/plan run），"
                "或用 /plan off 放弃后再重新规划。"
            )
        )
    # A new plan starts a fresh book: goal, items and receipts belong together,
    # or the new plan's step ids collide with the old plan's receipts.
    begin = getattr(session, "begin_plan", None)
    if begin is not None:
        begin(topic)
    #: The topic is what the human said, so it goes in as their own line:
    #: the transcript shows their words rather than the instruction.  The
    #: instruction the model acts on is the harness's, and carries the
    #: ``[plan] `` mark so the transcript can tell the two apart.
    #:
    #: The instruction is deliberately the *last* message: it is the nearest
    #: thing to the model's next step, and a planning round only works when
    #: the instruction is there (measured: the same instruction in the
    #: system prompt did not make the model plan).
    session.add_message(user_message(topic, now_utc(), images))
    with continuations_disabled(loop, max_iterations=PLANNING_MAX_ITERATIONS):
        await loop.process_input(
            session,
            f"{PLAN_MARK}{PLAN_PROMPT.format(topic=topic)}",
            # M2 T3 (Q4): the instruction is the harness's, so it is marked
            # as one.  It used to be told apart by its ``[plan] `` prefix --
            # a convention every reader had to learn; the mark is the same
            # answer the reminder gets.
            synthetic=True,
            on_delta=on_delta, observer=observer,
        )
    task = getattr(session, "task", None)
    items = getattr(task, "items", ())
    offenders = outside_workspace_paths(
        [item.content for item in items], workspace, extra_roots=extra_roots
    )
    if not offenders:
        offenders = outside_workspace_paths(
            [item.verify for item in items if item.verify],
            workspace,
            extra_roots=extra_roots,
            from_shell=True,
        )
    if offenders:
        # W2/R02: this gate runs *after* the human approved the plan, and
        # approval is what armed the task.  Refusing the plan must withdraw
        # that approval: an armed plan the runner would refuse anyway stays on
        # disk, and the boot resume would pick it up and run it.
        if task is not None and task.disarm():
            _persist_session(loop, session)
            _emit_task(observer, task)
        return PlanReport(stopped=outside_workspace_message(offenders))
    return PlanReport()


@contextmanager
def continuations_disabled(
    loop: Any, *, max_iterations: int | None = None
) -> Iterator[None]:
    """Run a block with C3 (auto-continuation) off, then restore the policy.

    ``max_iterations`` optionally swaps in a different iteration ceiling for
    the block — the planning round passes its own budget (19 §3.4-1); steps
    keep the shared ceiling untouched.

    C3 pays off when the model is finishing *its own* work and stopped only
    because the iteration budget ran out.  It does not pay off when something
    else is waiting on the turn:

    - the planning turn, whose open items are the plan being authored rather
      than work in progress.  Continuing it told the model to "keep going" on
      the one turn whose prompt says *do not execute the work* (measured: 10
      model calls and 3 ``[continue]`` injections where 2 calls did the job);
    - plan steps, which now carry the same one-continuation treatment as a
      scoped policy (``max_auto_continuations=1`` + ``step_item_id``), not this
      helper;
    - sub-agents, which a parent already waits on and which carry their own
      wall-clock ceiling (``subagent._subagent_settings``).

    Plain ``loop.policy`` access on purpose: a loop without a policy is not an
    AgentLoop, and quietly skipping the swap would leave C3 armed in exactly
    the turn this exists to disarm.
    """
    original = loop.policy
    overrides: dict[str, Any] = {"max_auto_continuations": 0}
    if max_iterations is not None:
        overrides["max_iterations"] = int(max_iterations)
    loop.policy = TurnPolicy(replace(original.config, **overrides))
    try:
        yield
    finally:
        loop.policy = original


class PlanRunner:
    """Execute a session's todo plan step by step through a loop.

    （11-plan T3）The round loop itself now lives in the kernel
    :class:`~ai_coding.core.task_driver.TaskDriver`; this class is the
    ``plan`` RoundProfile.  Every hook below is a verbatim wrapper of the
    piece the pre-T3 ``run`` body had, so the plan suites are the
    equivalence proof and nothing here may drift from those semantics.
    """

    def __init__(self, loop: Any) -> None:
        self._loop = loop
        #: T3: protocol signatures (`no_item`, `taskless`) cannot carry the
        #: session/receipt set that the old loop-top `_backfill_fiat` needed;
        #: `prepare` stashes them for that one call.  Safe because a runner
        #: instance serves exactly one run.
        self._run_session: Any = None
        self._run_receipted: set[str] = set()
        self._run_attempted: set[str] = set()

    async def run(
        self,
        session: Any,
        console: Any = None,
        *,
        on_delta: Any = None,
        observer: Any = None,
    ) -> PlanReport:
        """Run every ready todo item; return what happened (11-plan T3).

        The command-driven run arms the task for its duration (the driver's
        loop only drives armed tasks; boot-resume sessions arrive armed) and
        the driver disarms on every stop path — one lifecycle, whether the
        run came from the command, a boot resume, or a direct caller.
        """
        task = getattr(session, "task", None)
        # W2/R02: the runner's own workspace gate, before anything runs.
        #
        # This is what makes the refusal structural rather than advisory:
        # every way into a run -- the command, the front end's auto-dispatch,
        # the boot resume -- comes through this method, so a plan the file
        # tools cannot execute never reaches a turn.  The earlier gates catch
        # it earlier (before the human is asked), but a plan can also arrive
        # here from a session that was armed before those gates existed, or
        # from a task written by a tool that bypassed the review.
        #
        # The refusal has to withdraw the approval as well: the human's
        # approval is what armed the task, and an armed plan the runner
        # refuses would still be on disk for the boot resume to pick up.
        offenders = self._workspace_offenders(task)
        if offenders:
            refused = self.new_report()
            refused.stopped = outside_workspace_message(offenders)
            if task is not None and task.disarm():
                self._persist(session)
                self._emit_task(observer, task)
            return refused
        if task is not None:
            task.armed = True
            task.armed_profile = "plan"
            # 11-spec §4.2: arm is durable before any round runs (crash
            # point ①: a crash mid-run must leave the task resumable).  A
            # fiat-only run (all items already done) arms too — its single
            # loop pass is the backfill — and disarms on the way out.
            self._persist(session)
            # R06: the runner's own arm is a task mutation like any tool's —
            # a live card must see it (F-47).
            self._emit_task(observer, task)
        try:
            report: PlanReport = await TaskDriver(self._loop).run(
                session, self, console, on_delta=on_delta, observer=observer
            )
            return report
        finally:
            # 11-spec §4.2: every exit disarms — completion, a failing round,
            # the round cap, cancellation, or a mid-air Esc (whose own handler
            # disarmed already; the shared object makes both writes benign).
            # The driver disarms on its normal paths; this covers cancellation.
            if task is not None:
                if task.disarm():
                    self._persist(session)
                # R06: whichever layer disarmed (the driver's normal exit or
                # this finally), the final state reaches the wire.
                self._emit_task(observer, task)

    # ---- RoundProfile (11-plan §5.2): one hook per pre-T3 run-body piece --

    def new_report(self) -> PlanReport:
        return PlanReport()

    def max_rounds(self) -> int:
        """R1-4: the run's own step-turn ceiling (0 disables)."""
        return self._run_budget()

    def prepare(self, session: Any, task: Any) -> set[str]:
        """R1-5 receipts: drop foreign ones, hand back the session's own set.

        Receipts are keyed by step id and ids restart at 1 for every list, so
        a list replaced mid-turn (``present_plan`` approval replaces the list;
        sessions written before ``todo_write(clear)`` was removed could do the
        same) leaves receipts whose ids collide with the new list's.  A
        receipt describes the statement it settled, so anything whose
        statement is no longer on the list belongs to a plan that is gone --
        dropped here, before they can stand in for the new plan's steps.
        """
        self._run_session = session
        self._drop_foreign_receipts(session, task)
        self._run_receipted = {
            entry.step_id for entry in getattr(session, "step_deliveries", [])
        }
        return self._run_receipted

    def next_item(self, task: Any, attempted: set[str]) -> Any:
        # The driver mutates this set in place; keep the reference so
        # `no_item` can apply the same this-run rule the old loop used.
        self._run_attempted = attempted
        return self._next_ready(task, attempted)

    def before_round(
        self, task: Any, session: Any, report: PlanReport, receipted: set[str]
    ) -> None:
        """The old loop top's fiat backfill (R1-5)."""
        self._backfill_fiat(task, session, report, receipted)

    def no_item(self, task: Any, report: PlanReport) -> None:
        """The old loop top's last pass: backfill, then the deadlock rule.

        19 §3.4-3: a deadlock is "an open item we never even tried, and it
        cannot run" (missing dependency or cycle).  Open items we already
        attempted are stalls/blocks — the report carries them, and reporting
        "deadlock" there would be a lie.
        """
        self._backfill_fiat(task, self._run_session, report, self._run_receipted)
        never_attempted = [
            entry for entry in task.open_items() if entry.id not in self._run_attempted
        ]
        if never_attempted:
            report.stopped = (
                "deadlock: open items remain but none is ready "
                "(missing dependency or cycle)"
            )

    def rounds_used(self, report: PlanReport) -> int:
        return report.turns_used

    def budget_stop(self, report: PlanReport, cap: int) -> None:
        """R1-4: the ceiling was reached with work left — not a clean end."""
        report.stopped = (
            f"budget: plan turn budget exhausted "
            f"({report.turns_used}/{cap} step-turns); "
            "remaining steps stay open"
        )

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
        await self._run_step(
            session, task, item, report, console, on_delta=on_delta, observer=observer
        )

    def after_round(self, session: Any, task: Any, report: PlanReport) -> None:
        """11-spec §5.1: count the round and persist (crash point ②: a crash
        between rounds leaves ``armed`` + ``roundsRun`` on disk, so the boot
        scan sees the task and the breaker counts correctly)."""
        task.rounds_run = int(getattr(task, "rounds_run", 0) or 0) + 1
        self._persist(session)

    def should_stop(self, report: PlanReport, item: Any) -> bool:
        """r22: one failed step must not end the whole run.

        A blocked step is recorded and its dependents stay unrunnable (the
        driver's ``next_item`` only considers ready items), but *independent*
        steps still have work to do.  The old rule stopped at the first
        blocked step -- measured in the hotel session, whose plan verified one
        step with a wrong path and then never touched the remaining ones, so
        the operator had to drive every step by hand.  The run now ends on
        done/deadlock/cap and its report lists what was blocked.
        """
        return False

    def taskless(self, report: PlanReport, console: Any) -> None:
        """No task at all (a bare /plan on a session that never had one):
        same verdict as an empty plan — "nothing to do", not "run /plan
        first", which would be absurd right after planning."""
        report.stopped = "no-open-items"
        self._say(console, "[plan] 完成 0 步 · 用了 0 回合（无任务项）")

    def persist(self, session: Any) -> None:
        self._persist(session)

    def finish(self, console: Any, report: PlanReport) -> None:
        self._say(console, report.render())

    # ---- internals ------------------------------------------------------

    def _workspace_offenders(self, task: Any) -> list[str]:
        """The open steps that name a path the file tools cannot reach.

        The open items are what the run would execute, so they are what the
        gate has to look at.  A step already completed or dropped names
        nothing the run would touch.
        """
        if task is None:
            return []
        workspace = str(getattr(self._loop, "workspace", "") or ".")
        extra_roots = tuple(getattr(self._loop, "extra_workspace_roots", ()) or ())
        items = task.open_items()
        offenders = outside_workspace_paths(
            [item.content for item in items],
            workspace,
            extra_roots=extra_roots,
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
        return int(getattr(settings, "plan_step_max_iterations", 100) or 100)

    def _make_step_session(
        self, session: Any, task: Any, item: Any, report: PlanReport
    ) -> SessionData:
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
            ChatMessage(
                role="user",
                content=self._seed(session, task, item, report),
                timestamp="",
                synthetic=True,
            )
        ]
        return step

    def _seed(self, session: Any, task: Any, item: Any, report: PlanReport) -> str:
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
        # "[计划标题]" not "[计划目标]": ``plan.updated`` also carries a
        # ``goal`` key, and a step reading "目标" went looking for a running
        # goal (``start_goal``/``update_goal`` are for R21 §3).  A plan has a
        # title; a goal has a lifetime.  The two words must not collide.
        lines = [f"[计划标题] {task.goal or '(未命名计划)'}", "", "[计划步骤]"]
        for entry in task.items:
            if entry.is_terminal():
                continue
            marker = " ← 本步" if entry.id == item.id else ""
            lines.append(f"{entry.id}. [{entry.status}] {entry.content}{marker}")
        if item.verify:
            lines += ["", f"[本步验收] {item.verify}"]
        prior = [
            entry for entry in getattr(session, "step_deliveries", [])
            if entry.statement == item.content
            and entry.status in ("stalled", "blocked")
        ]
        if prior:
            # 19 §3.4-4: a retry must not replay the whole attempt — say where
            # the last one stopped (the receipt's own summary) and forbid a
            # restart from zero (measured: two runs of the same step re-read
            # the same file from offset 0).
            last = prior[-1]
            lines += [
                "",
                f"[本步历史] 此前已尝试 {len(prior)} 次；"
                f"上次结果（{last.status}）：{last.summary}",
                "从上次停下的地方继续，不要从头再来。",
            ]
        return "\n".join(lines)

    def _run_budget(self) -> int:
        """Turn ceiling for this run (R1-4): 0 (or negative) disables."""
        settings = getattr(self._loop, "settings", None)
        value = getattr(settings, "plan_max_step_turns", 0)
        if value is None:
            return 0
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

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
        # M3: the step's live output goes into a view of its own, closed with
        # the receipt summary — the same sentence /resume will show.  Optional:
        # a console that cannot open one (or none at all) still runs the plan.
        step_view = self._open_step_view(console, item, total)
        step_delta = step_view.delta if step_view is not None else on_delta
        # M1-B: the step runs in its own short conversation, not the parent's
        # ever-growing history.  The parent keeps only the prompt (replay
        # marker) and a one-line summary.
        step_session = self._make_step_session(session, task, item, report)
        seed = step_session.messages[0].content or ""
        contracts = (
            [AcceptanceItem(
                id=f"plan-{item.id}", statement=item.content, verify=item.verify,
            )]
            if item.verify else []
        )
        original_policy = self._loop.policy
        budget = self._step_budget()
        # One wall clock for this item; its repair turn shares it (the old
        # behaviour bought a fresh ``turn_timeout_seconds`` per call).
        item_limit = float(
            getattr(getattr(self._loop, "settings", None), "plan_item_wall_seconds", 0.0)
            or 0.0
        )
        step_session.item_deadline = (
            Deadline.after(item_limit) if item_limit > 0 else None
        )
        # M1-B: close the todo gate too — a step must not be nudged about the
        # plan's other open items.  Same swap-and-restore discipline as before.
        # C3: one continuation, and only because ``step_item_id`` narrows the
        # open set to this item — the step session shares the plan's task, so
        # without a scope one continuation would spend the whole plan's list.
        step_session.step_item_id = str(getattr(item, "id", "") or "")
        self._loop.policy = TurnPolicy(
            replace(original_policy.config, max_iterations=budget,
                    stop_policy="natural", max_auto_continuations=1)
        )
        attempts = 0
        step_usage: dict[str, int] | None = None
        step_summary: str | None = None
        # Built before the try: a step that dies mid-turn still has to leave a
        # replay marker on the parent, and the marker is this prompt.
        step_prompt = self._step_prompt(item)
        try:
            step_session.acceptance = list(contracts)
            start_turn = int(getattr(step_session, "turn_count", 0) or 0)
            text = await self._loop.process_input(
                step_session, step_prompt,
                on_delta=step_delta, observer=observer, synthetic=True,
            )
            attempts = 1
            # R1-4: every returned turn costs budget, and its spend is read
            # off the loop while it is still "the last turn".
            step_usage = self._charge_turn(report, step_usage)
            if contracts and not self._contracts_passed(step_session):
                # The repair turn is this item's last attempt.  C3 grants one
                # continuation *per turn* (the counter resets in
                # ``_begin_turn``), so leaving it armed would add another 100
                # iterations on top of 100+100 (first attempt) + 100 (repair).
                self._loop.policy = TurnPolicy(
                    replace(self._loop.policy.config, max_auto_continuations=0)
                )
                text = await self._loop.process_input(
                    step_session, self._repair_prompt(item, step_session),
                    on_delta=step_delta, observer=observer, synthetic=True,
                )
                attempts = 2
                step_usage = self._charge_turn(report, step_usage)
            # A budget-killed turn no longer gets its own continuation call:
            # the loop's C3 already continues once inside that turn, scoped to
            # this item (``step_item_id``).  Two mechanisms meant 2 attempts x 2
            # rounds = 400 iterations for one step.
            end_turn = int(getattr(step_session, "turn_count", 0) or 0)
            delivery = self._settle(step_session, session, task, item, report, text,
                                   attempts, (start_turn, end_turn), step_usage,
                                   len(seed), observer=observer)
            step_summary = delivery.summary
            self._record_on_parent(session, step_prompt, step_summary, observer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad step must not kill the run
            task.update(item.id, status="blocked",
                        evidence=f"runner error: {exc}")
            # R06: the blocked verdict is a task mutation; the wire sees it.
            self._emit_task(observer, task)
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
            step_summary = delivery.summary
            self._record_on_parent(session, step_prompt, step_summary, observer)
        finally:
            self._loop.policy = original_policy
            if step_view is not None:
                step_view.end(step_summary)

    @staticmethod
    def _record_on_parent(
        session: Any, prompt: str, summary: str, observer: Any = None
    ) -> None:
        """Leave the parent's two-bubble trace of one step, and say so (E2).

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
        # D6: the step head is the harness's replay marker, not something the
        # operator said.  Stamped, request assembly / undo / compaction leave
        # it alone, and replay can skip it by the class rather than a prefix.
        session.add_message(
            ChatMessage(role="user", content=prompt, timestamp=now, synthetic=True)
        )
        # 08 T1b: the execution layer's bubble is the *build* agent's report,
        # not the plan agent's — the step ran as work, so it must not claim to
        # be the planner's words.
        session.add_message(
            ChatMessage(
                role="assistant", content=summary, timestamp=now, agent="build"
            )
        )
        if observer is not None:
            # Observation only: a client that cannot render this must not cost
            # the step its receipt (the bubbles are already written).
            #
            # ``on_event`` -- the LoopObserver protocol -- not a bare call:
            # the callable spelling this used to have raised TypeError against
            # the real bridge (it has on_event, no __call__) and the except
            # below swallowed it, so the receipt was dead in production while
            # the runner tests, which passed a list.append, stayed green
            # (found 2026-10-17 while wiring G2's task emissions through the
            # same seam; pinned by the boot-resume IPC test's plan.receipt
            # count).
            try:
                observer.on_event(PlanStepReceipt(summary=summary))
            except Exception:  # noqa: BLE001 — see above
                pass

    @staticmethod
    def _open_step_view(console: Any, item: Any, total: int) -> Any:
        """Ask the console for a view of one step, or ``None`` if it has none.

        Duck-typed on purpose: the runner is handed whatever console the UI
        passes (a Textual one, a dialogue one, or nothing at all in tests), and
        a console that cannot show a step must not stop a plan from running.
        """
        opener = getattr(console, "step", None)
        if opener is None:
            return None
        return opener(f"[plan] 步骤 {item.id}/{total}：{item.content}")

    def _contracts_passed(self, step_session: Any) -> bool:
        return bool(step_session.acceptance) and all(
            item.passed is True for item in step_session.acceptance
        )

    def _settle(self, step_session: Any, parent: Any, task: Any, item: Any, report: PlanReport,
                text: Any, attempts: int, turns: tuple[int, int],
                usage: dict[str, int] | None, seed_chars: int,
                observer: Any = None) -> StepDelivery:
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
                self._emit_task(observer, task)
            else:
                task.update(item.id, status="blocked",
                            evidence=f"verify failed: {_tail(self._verify_output(step_session))}")
                self._emit_task(observer, task)
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

    @staticmethod
    def _emit_task(observer: Any, task: Any) -> None:
        """R06 (F-47): the runner's own task mutations travel the same wire the
        model's tool calls already use, so a live card never freezes at the
        moment of approval.  None-safe: a bare caller stays silent."""
        if observer is not None and hasattr(observer, "on_event"):
            observer.on_event(TaskUpdated(task=task))

    def _delivery(self, item: Any, status: str, attempts: int,
                ac: list[dict[str, Any]], summary: str,
                turns: tuple[int, int], usage: dict[str, int] | None = None,
                seed_chars: int = 0, *, attested: bool = True) -> StepDelivery:
        return StepDelivery(
            step_id=item.id, statement=item.content, status=status,
            attempts=attempts, ac=ac, summary=summary, turns=turns,
            usage=usage, seed_chars=seed_chars, attested=attested,
        )

    def _step_prompt(self, item: Any) -> str:
        return (
            f"{STEP_HEAD}{item.content}\n"
            f"（这是计划步骤 {item.id}，做完停下等验收，不要展开做别的。）"
        )

    def _charge_turn(
        self, report: PlanReport, step_usage: dict[str, int] | None
    ) -> dict[str, int] | None:
        """Count one returned turn onto the run and onto the step (R1-4)."""
        report.turns_used += 1
        turn_usage = self._turn_usage()
        report.tokens_used = self._merge_usage(report.tokens_used, turn_usage)
        return self._merge_usage(step_usage, turn_usage)

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


__all__ = ["PLAN_PROMPT", "PlanReport", "PlanRunner", "STEP_HEAD",
           "continuations_disabled", "plan_only"]
