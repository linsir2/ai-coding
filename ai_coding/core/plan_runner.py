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
import re
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ai_coding.core.events import PlanStepReceipt, TaskUpdated
from ai_coding.core.file_mentions import FILE_PATH_EXTENSIONS
from ai_coding.core.task_driver import TaskDriver
from ai_coding.core.turn_policy import TurnPolicy
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.loop import TurnStatus
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.plan_delivery import StepDelivery
from ai_coding.domain.run import user_message
from ai_coding.domain.session import SessionData
from ai_coding.infra.time_utils import now_utc
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.shell_parse import path_candidates as _shell_path_candidates

#: Step prompts start here.  The replay matches user bubbles on this marker
#: to rebuild step headers — turn numbers do not survive compaction, but the
#: prompt text travels inside the persisted user message.
#: The mark on the harness's own line for a planning round.  The frontend
#: skips a user message carrying it (see ``state/replay.ts``), so the
#: transcript shows the human's words and not the instruction.  It is the
#: same convention the other harness lines use (``[notice] ``, ``[steering] ``).
PLAN_MARK = "[plan] "

STEP_HEAD = "只做这一步，不要做别的："

#: 19 §3.4-1: the planning round's own iteration ceiling.  It used to share
#: ``max_tool_iterations`` and died mid-plan (measured twice: 15 iterations
#: eaten by large reads, a half-written plan reported as ready).  The ceiling
#: is headroom; the prompt guidance ("skeleton first, no whole-file reads")
#: is the actual fix.
PLANNING_MAX_ITERATIONS = 30

#: Cap on the failure output quoted into a repair prompt (same 500-char
#: preview discipline as the acceptance evidence tails).
_OUTPUT_TAIL = 500


def _tail(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _OUTPUT_TAIL else f"…{text[-_OUTPUT_TAIL:]}"


#: The planning prompt.  One definition: it used to be written out twice (the
#: Textual front-end and the REPL dispatcher), which is how the two drifted.
PLAN_PROMPT = (
    "Create a detailed implementation plan for: {topic}. "
    "Break it down into specific, actionable steps. "
    "Submit the plan with present_plan: a concise markdown summary (a title "
    "first) and, in its items array, every step — each item carries its proof "
    "command in 'verify' (a shell command) when the outcome is "
    "machine-checkable, so the plan can be executed and verified step by "
    "step. Do not track the planning with todo_write. "
    "Write the items as pending/open steps only — do not execute "
    "the work and do not mark items completed during planning. "
    "State each step's goal and its verifiable success criteria, "
    "and make the plan decision-complete: another engineer should be "
    "able to implement it without making further design decisions. "
    "Write the full skeleton of items first (one line plus its verify for "
    "each) and refine afterwards; use grep/glob to look at code selectively "
    "— do not read whole large files just to write the plan. "
    "bash/write/edit are unavailable in plan mode — explore with "
    "read/grep/glob instead. "
    "present_plan presents the plan for the user's review, and approval "
    "starts execution automatically — do not execute it yourself. "
    "If the topic needs no implementation — a survey, an analysis, a "
    "comparison — close it with finish_research(summary) instead of a plan. "
    "A request that asks for changes (implement, modify, add, fix, refactor) "
    "needs a plan, not a conclusion."
)


# Conservative on purpose: only well-known filesystem roots are candidates, and
# a leading word/colon is excluded so URLs and prose never match.  The gate
# exists because file tools refuse outside paths anyway; failing at plan time
# beats seventeen failed calls (hotel-session audit, root cause R2).
#
# W2/R02 replaced the root list: a mount point the operator never named
# (``/data``, ``/workspace``) was invisible to it, so a plan naming one
# sailed through.  The candidate is now *any* absolute path -- which roots
# exist is not the question; reachability is.  Reachability is answered by
# :class:`Sandbox`, which resolves for real, so the scan no longer has to
# guess which prefixes might be roots.
#
# The lookbehind keeps three things out: a path glued to a word (``x/home``),
# a URL's authority (``https://host/a/b``), and a relative path (``../sibling``
# and ``./config`` are relative, and the leading ``.`` is what says so).  What
# is left is a string that *could* be a path, which :func:`_looks_like_path`
# then has to agree with.
_OUTSIDE_PATH_RE = re.compile(r"(?<![\w:./])(?:/[^/\s'\"`,;)\]]+){2,}")

#: The same "is this a file path?" question :mod:`ai_coding.core.file_mentions`
#: asks of a pasted path, asked here of a path a plan names.  One answer for
#: both: a known source/document extension, or a first segment that is a real
#: directory on this machine.
#:
#: Without it the scan reads an HTTP route (``/api/v1/users``) as a file
#: system path and refuses the plan -- a route names a resource, not a place
#: on disk, and a plan may legitimately name one.
#:
#: The dot before the extension is part of the answer, not a detail: without
#: it ``users`` ends in ``rs`` (Rust) and every route ending in a two-letter
#: extension would read as a file.  ``file_mentions`` requires the dot for the
#: same reason.
_FILE_SUFFIX_RE = re.compile(rf"\.(?:{FILE_PATH_EXTENSIONS})$", re.IGNORECASE)

#: The words that make the path in front of them a *directory*.  Used only
#: for the trailing-slash form, which on its own cannot be told apart from a
#: route (``/api/v1/users/`` and ``/data/w2demo/`` have the same shape).
#:
#: The live check found why this needs the words: the model writes a
#: directory target as "确保目录 /data/w2demo/ 存在" and a route as
#: "GET /api/v1/users/ 接口" -- the same shape, different surroundings.
#: Reading only the slash would refuse the route, and a refused plan is a
#: dead end the user cannot get around.
#:
#: The word has to be *right next to* the path, separated by nothing but
#: space or a quote.  "目录" also means a table of contents, and "目录结构
#: 如下 /api/v1/" is a document's outline, not a directory to create --
#: there the words between the word and the path belong to the other
#: meaning, and the gap is what tells the two apart.
#:
#: A command's own options may sit in the gap ("mkdir -p /data/x"): the
#: option says how the directory is made, not that it is somewhere else.
_DIRECTORY_BEFORE_RE = re.compile(
    r"(?:目录|文件夹|创建目录|新建目录|建立目录|mkdir|directory|folder)"
    r"(?:\s+-{1,2}[A-Za-z-]+)*"
    r"[\s`'\"]*$",
    re.IGNORECASE,
)

#: A shebang marker.  What follows it is the interpreter that runs the
#: script, which is not a file the tools would write -- and a plan that
#: writes a script says so.  The marker may be quoted into a printf rather
#: than written at the start of a line, so it is looked for anywhere.
_SHEBANG_MARKER = "#!"


def _looks_like_path(candidate: str, *, directory: bool = False) -> bool:
    """Does this absolute path name something on the file system?

    Three answers, all borrowed from the repository's own sense of what a
    path is (``core/file_mentions.py``):

    * the last segment names a known source or document (``app.py``);
    * the first segment is a directory that exists here (``/etc/passwd``,
      ``/usr/local/bin/tool``);
    * the path was written as a directory, and the words around it say so
      (``确保目录 /data/w2demo/ 存在``).

    The third is the one the live check found the hard way: the model writes
    a directory target the way a person does -- with no extension, under a
    first segment the machine has never heard of.  A gate that recognised
    only files let that plan through, and the review card went out for a
    plan the file tools cannot execute.

    A path that is none of these is prose that happens to be shaped like one:
    an API route, a version number, a fraction.  Those are not places, and
    refusing a plan over them would be a false alarm.
    """
    if directory:
        return True
    if _FILE_SUFFIX_RE.search(candidate.rsplit("/", 1)[-1]):
        return True
    return Path("/" + candidate.split("/")[1]).is_dir()


def outside_workspace_paths(
    texts: Iterable[str],
    workspace: str,
    *,
    extra_roots: Sequence[str] = (),
    from_shell: bool = False,
) -> list[str]:
    """Absolute filesystem paths in ``texts`` that file tools cannot reach.

    ``extra_roots`` are the directories the operator declared
    (``tools.extra_workspace_roots``).  Without them this function answered
    "can a file tool reach this?" with a :class:`Sandbox` built from the
    workspace alone, so a directory the operator had already authorized was
    reported as unreachable -- and the message tells the model to switch
    workspaces for a path that already works.

    ``from_shell`` says the text is a shell command rather than prose, and
    it changes where the candidates come from.  A command is read by the
    shell grammar (:mod:`ai_coding.shell_parse`), which sees paths
    that a text scan cannot -- the one behind a variable
    (``D=/data/x; rm -rf $D``), the one inside a command substitution
    (``$(cat /data/secret)``), the one in a directory command
    (``mkdir -p /data/w2demo``).  The grammar confirms those are words in
    a command, so the shape heuristic (:func:`_looks_like_path`) has
    nothing left to say about them and is skipped: a word that starts at
    the root *is* a path.

    Prose is the other way round.  A plan's sentence is not a command, the
    grammar can only read fragments out of it, and the shape heuristic is
    what keeps "新增 GET /api/v1/users 接口" from turning into a
    workspace refusal.
    """
    sandbox = Sandbox(workspace or ".", extra_roots=tuple(extra_roots))
    offenders: list[str] = []
    for text in texts:
        body = str(text or "")
        interpreters = _shebang_interpreters(body)
        candidates: list[tuple[str, bool]] = []
        for match in _OUTSIDE_PATH_RE.finditer(body):
            candidates.append((match.group(0).rstrip(".,"), False))
        if from_shell:
            for candidate in _shell_path_candidates(body):
                candidates.append((candidate, True))
        for candidate, from_grammar in candidates:
            if candidate in offenders:
                continue
            # A shebang names the interpreter that runs a script, not a file
            # the tools would write.  The live plan carried
            # "#!/usr/bin/env python3" inside the script it wrote, and the
            # gate reported the interpreter as unreachable -- so the refusal
            # named a path the model had no reason to change, and it retried
            # the same plan thirteen times before it moved on.
            #
            # Only the interpreter itself: a shebang exempts the first path
            # after it, not every path after it (a plan that writes a script
            # may well write another file too, and that one is a target).
            if candidate in interpreters:
                continue
            if not from_grammar:
                # A trailing slash says "directory" -- and so do the words
                # around the path: the model writes a directory target as
                # "确保目录 /data/w2demo/ 存在", and a command as
                # "mkdir -p /data/w2demo".  A route written with a trailing
                # slash ("GET /api/v1/users/") has the same shape and no
                # directory words around it, so it stays what it is.
                directory = _has_directory_context(body, match.start())
                if not _looks_like_path(candidate, directory=directory):
                    continue
            try:
                sandbox.resolve(candidate)
            except SandboxViolation:
                offenders.append(candidate)
    return offenders


def _shebang_interpreters(body: str) -> set[str]:
    """The interpreter paths the shebangs in this text name.

    A shebang (``#!``) says "this interpreter runs that script", which is a
    different thing from "the file tools touch this": the interpreter is
    read by the kernel, not written by a plan.  The live plan carried
    ``#!/usr/bin/env python3`` inside the script it wrote, and the gate
    reported the interpreter as an unreachable target -- so the refusal named
    a path the model had no reason to change, and it retried the same plan
    thirteen times before it moved on.

    Each shebang exempts the first path after it and no other: a plan that
    writes a script may write another file too, and that one is a target.
    """
    interpreters: set[str] = set()
    start = 0
    while True:
        marker = body.find(_SHEBANG_MARKER, start)
        if marker < 0:
            return interpreters
        after = _OUTSIDE_PATH_RE.search(body, marker + len(_SHEBANG_MARKER))
        if after is None:
            return interpreters
        interpreters.add(after.group(0).rstrip(".,"))
        start = after.end()


def _has_directory_context(body: str, start: int) -> bool:
    """Do the words just before this path say it is a directory?

    The live check is what made this necessary: the model writes a directory
    target as "确保目录 /data/w2demo/ 存在" and a route as "GET /api/v1/users/
    接口" -- the same shape, different words around them.  The words are read
    from the text just before the path, and only the ones that name a
    directory count: a route's own words ("接口", "端点", "路由") say the
    opposite.
    """
    return _DIRECTORY_BEFORE_RE.search(body[:start]) is not None


def outside_workspace_message(paths: Sequence[str]) -> str:
    """What the caller tells the model when a plan names an unreachable path."""
    return (
        "目标路径在工作区外：" + ", ".join(paths)
        + "。文件工具（read/write/edit/glob）无法访问工作区外的路径；"
        "请 cd 到目标目录重新启动 ai-coding，或把计划改为工作区内的路径。"
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
            session, f"{PLAN_MARK}{PLAN_PROMPT.format(topic=topic)}",
            on_delta=on_delta, observer=observer,
        )
    task = getattr(session, "task", None)
    items = getattr(task, "items", ())
    offenders = outside_workspace_paths(
        [item.content for item in items], workspace, extra_roots=extra_roots
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
    - plan steps, which get the same treatment inline in ``_run_step``;
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
                # R06: same wire, the other half of the lifecycle.
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
        """19 §3.4-3 (Q5=A): a stalled step is recorded, not fatal — the run
        moves to the next ready item and the report lists the stalls for a
        re-run.  Blocked stays fatal for the item at hand: a failed proof is
        the decision point this runner exists to surface."""
        return bool(report.blocked) and report.blocked[-1] == item.id

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
        return outside_workspace_paths(
            [item.content for item in task.open_items()],
            workspace,
            extra_roots=extra_roots,
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
        return int(getattr(settings, "plan_step_max_iterations", 8) or 8)

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
        lines = [f"[计划目标] {task.goal or '(未命名计划)'}", "", "[计划步骤]"]
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
        step_summary: str | None = None
        # Built before the try: a step that dies mid-turn still has to leave a
        # replay marker on the parent, and the marker is this prompt.
        step_prompt = self._step_prompt(item)
        try:
            step_session.acceptance = list(contracts)
            start_turn = int(getattr(step_session, "turn_count", 0) or 0)
            text = await self._loop.process_input(
                step_session, step_prompt,
                on_delta=step_delta, observer=observer,
            )
            attempts = 1
            # R1-4: every returned turn costs budget, and its spend is read
            # off the loop while it is still "the last turn".
            step_usage = self._charge_turn(report, step_usage)
            if contracts and not self._contracts_passed(step_session):
                text = await self._loop.process_input(
                    step_session, self._repair_prompt(item, step_session),
                    on_delta=step_delta, observer=observer,
                )
                attempts = 2
                step_usage = self._charge_turn(report, step_usage)
            elif self._budget_died() and not self._model_claimed_done(task, item):
                # 19 §3.4-2: the budget cut this step mid-work (the model was
                # still going — "继续精读…" in the measured run), so it gets one
                # bounded continuation.  Shares the single repair allowance
                # with the verify path above; the two never stack.
                text = await self._loop.process_input(
                    step_session, self._continue_prompt(item),
                    on_delta=step_delta, observer=observer,
                )
                attempts = 2
                step_usage = self._charge_turn(report, step_usage)
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
        session.add_message(ChatMessage(role="user", content=prompt, timestamp=now))
        session.add_message(ChatMessage(role="assistant", content=summary, timestamp=now))
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
                observer.on_event(PlanStepReceipt(prompt=prompt, summary=summary))
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

    def _charge_turn(
        self, report: PlanReport, step_usage: dict[str, int] | None
    ) -> dict[str, int] | None:
        """Count one returned turn onto the run and onto the step (R1-4)."""
        report.turns_used += 1
        turn_usage = self._turn_usage()
        report.tokens_used = self._merge_usage(report.tokens_used, turn_usage)
        return self._merge_usage(step_usage, turn_usage)

    def _budget_died(self) -> bool:
        """True when the last step turn was cut by the iteration budget (19 §3.4-2).

        ``TurnStatus``, not a reason string: the enum is what the loop reports
        against.  Stubs without the field read as "no" — fail-open to the old
        stall semantics.
        """
        outcome = getattr(self._loop, "last_outcome", None)
        return getattr(outcome, "status", None) is TurnStatus.MAX_ITERATIONS

    def _continue_prompt(self, item: Any) -> str:
        return (
            f"{STEP_HEAD}{item.content}\n"
            f"上一步预算用尽但本步未完成：从停下的地方继续，只做这一步；完成后简要汇报。"
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


__all__ = ["PLAN_PROMPT", "PlanReport", "PlanRunner", "STEP_HEAD",
           "continuations_disabled", "plan_only"]
