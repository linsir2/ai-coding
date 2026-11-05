"""Everything one turn counts about itself (R7a).

The loop's body used to carry nine loose locals — ``iterations``, ``nudges``,
``attempted_calls``, ``successful_calls``, ``failed_streak``, ``strategy_nudges``,
``failures``, ``last_text`` and ``summaries`` — spread across a 228-line
``process_input``.  R6 added four of them (the escalation ladder needs to know how
the tool calls are going) and that is exactly why the method grew back after R5
had split it: every new behaviour had to reach into the same local scope.

Two values shape a turn, and they are deliberately different things:

* :class:`TurnFrame` is what the **prologue fixed** — the turn number, the task
  list, what ``turn.start`` hooks injected, the turn clock.  Nothing in an
  iteration may change it;
* :class:`TurnBooks` is what the **iterations counted** — the nine counters.

Both are pure: no IO, no asyncio, no events.  The loop notifies the books
(``note_*``) and asks them for the one object a decision needs (``snapshot``); the
rules stay table-testable in ``tests/test_turn_books.py``.

The accounting rules are the ones R6 measured and argued for, moved verbatim:

* an **empty batch** (a plan-only step) is not a failure streak;
* a batch with **anything successful** resets the streak;
* a failure is recorded as ``(tool, error-or-"failed", first 160 chars of output)``
  — the material ``render_handoff`` quotes back to the user.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_coding.core.dispatch import DispatchOutcome
from ai_coding.core.turn_policy import TurnSnapshot
from ai_coding.domain.ledger import UncertaintyItem, build_ledger
from ai_coding.domain.message import CANCELLED_BY_USER
from ai_coding.domain.task import TaskState, TodoItem
from ai_coding.domain.turn_digest import DEFAULT_NOTE_CHARS, one_line
from ai_coding.infra.deadline import Deadline

__all__ = [
    "ACTION_TOOLS",
    "RETRIEVAL_TOOLS",
    "STATE_ONLY_TOOLS",
    "Failure",
    "TurnBooks",
    "TurnFrame",
]

#: One failed call, as the handoff reports it: (tool, error, output excerpt).
Failure = tuple[str, str, str]

#: Tools whose *success* is not evidence about the world (R8a, D-14): writing a
#: checkpoint tells the turn about itself, so a turn that only did that is still
#: a "model" turn as far as the harvest's provenance is concerned.
STATE_ONLY_TOOLS = frozenset({"checkpoint_write"})

#: B1/B2: tools whose contract is "look at existing state".  ``bash`` is
#: deliberately absent: the static check itself runs through it, so a model that
#: follows the search ladder's advice must be *rewarded* (see ``ACTION_TOOLS``),
#: not pushed further up the ladder.
RETRIEVAL_TOOLS = frozenset(
    {
        "read",
        "grep",
        "glob",
        "history_search",
        "memory_search",
        "websearch",
        "webfetch",
        "job_output",
        "job_list",
        "subagent_list",
    }
)

#: B1/B2: what counts as *acting*.  An allowlist, not "anything that is not a
#: look": ``todo_write``, ``checkpoint_write``, ``ask_user`` and the goal tools do
#: not change the workspace, and letting them reset the streak would hand the model
#: a one-call way to hide a stall from the ladder.  ``bash`` is here on purpose --
#: running the static check is an action, and the ladder must see it that way.
#: ``mcp_call`` and ``subAgent`` are here because both can change the world and the
#: harness cannot tell which way they went; assuming "not an action" would keep the
#: streak climbing after real work.
ACTION_TOOLS = frozenset(
    {
        "write",
        "edit",
        "bash",
        "mcp_call",
        "subAgent",
        # A successful plan/task update is progress, not another look.  Without
        # this, update_plan left the search streak armed and the next iteration
        # injected the look-only gate forever.
        "update_plan",
    }
)

#: Errors that are verdicts rather than breakage (R8c D-11 cancel; 20 §批次3
#: plan-validation refusals -- ``update_plan``'s ``invalid_plan``).  A round
#: whose calls all carry these is not evidence of failure: it neither records
#: a failure nor keeps the burn -- the strategy window's helped-signal is
#: ``failed_streak == 0``, so an all-neutral round must reset (24d7109b: a
#: parked validation refusal kept the L1 window "failed" and L2 spent its
#: nudge on a duplicate report).
#:
#: ``guard_denied`` is deliberately NOT here, though it looks like the same kind
#: of thing.  Measured: exempting it makes the strategy ladder read a refusal as
#: *progress*.  A refused call still means the model did not get what it asked
#: for, so ``failed_streak`` should keep its burn; making it neutral hands the
#: loop a streak of 0, the L1 window closes "helped", and the same L1 nudge is
#: re-injected every iteration until the iteration budget dies -- a worse ending
#: than the one it was meant to prevent.  The fix for an over-eager guard is to
#: stop the guard firing on calls that produced nothing to reuse
#: (``guard.DuplicateCallGuard``), not to discount the refusal afterwards.
_NOT_EVIDENCE_ERRORS = frozenset({CANCELLED_BY_USER, "invalid_plan"})

# F2: Paris' Law fatigue tracking — accumulated error damage.
_FATIGUE_C = 1.0
_FATIGUE_M = 2.0

_SEVERITY_KEYWORDS: dict[int, tuple[str, ...]] = {
    5: ("timeout", "context overflow", "out of memory", "token limit"),
    4: ("rate limit", "too many requests", "quota exceeded"),
    3: ("permission denied", "access forbidden", "unauthorized"),
    2: ("not found", "invalid", "malformed", "bad request", "error", "failed"),
}

_FAILURE_EXCERPT = 160

#: One line of ``read``'s numbered output: six right-aligned digits, two spaces,
#: then the content.  The coverage check reads only the numbers, so a content
#: line that happens to start with digits cannot widen a range on its own.
_READ_NUMBERED = re.compile(r"^\s*(\d+)\s{2}")

#: I1/R-7: retrieval tools whose output can be turned into a *finding* key.
#: ``memory_search``/``history_search``/``websearch`` are deliberately absent:
#: their outputs embed the query, so changing the wording looks like news.
#: ``subagent_list`` is absent too -- its output is the harness's own bookkeeping.
_FINDING_TOOLS = frozenset({"read", "grep", "glob", "webfetch", "job_output", "job_list"})

#: I1/R-7: the `seen_findings` ceiling.  Overflow clears the set (worst case one
#: false negative) rather than evicting (which could admit a false positive).
_EVIDENCE_LIMIT = 4096

#: What a tool renders when it found nothing -- evidence of *absence*, not content.
_NO_MATCH = "(no matches)"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _output_digest(output: str) -> str:
    """A content fingerprint that ignores line order.

    ``find | sort`` and a plain ``find`` can describe the same tree; comparing
    raw bytes would call the second one "new evidence" and miss the loop.
    """
    lines = sorted(line for line in (output or "").splitlines())
    return _digest("\n".join(lines))


def _target(params: Mapping[str, Any]) -> str:
    """The root a search was aimed at -- so two empty results are different keys."""
    return str(params.get("path") or params.get("pattern") or "")


def _finding_key(name: str, params: Mapping[str, Any], output: str) -> str | None:
    """Normalise one retrieval result into an evidence key (I1/R-7).

    The rules are the plan's, each bought with a measured counter-example
    (docs/plan-m-b.md section 2): the tool name is part of every key (grep's and
    glob's ``(no matches)`` are byte-identical), grep keeps only ``path:line``
    (its hit text is clipped at 300 chars), glob hashes a *sorted* path set (it
    renders by mtime), and empty results keep the target they were aimed at.
    """
    body = output or ""
    if name == "read":
        path = str(params.get("path", "") or "")
        if not body.strip():
            return f"read:empty@{path}"
        return f"read:{path}:{_digest(body)}"
    if name in ("grep", "glob"):
        target = _target(params)
        lines = [
            line
            for line in body.splitlines()
            if line and not line.startswith("... [showing")
        ]
        if not lines or body.strip() == _NO_MATCH:
            return f"{name}:nomatch@{target}"
        if name == "grep":
            # Every hit is "path:line: text"; the text is the clipped part.
            units = sorted({
                ":".join(line.split(":", 2)[:2]) if ":" in line else line
                for line in lines
            })
            return f"grep:{_digest(chr(10).join(units))}"
        return f"glob:{_digest(chr(10).join(sorted(lines)))}"
    if not body.strip():
        return f"{name}:empty@{_target(params)}"
    return f"{name}:{_digest(body)}"


def _has_payload(output: str) -> bool:
    """True when a retrieval found something (an empty result is not a finding)."""
    return bool((output or "").strip()) and (output or "").strip() != _NO_MATCH


@dataclass(frozen=True)
class TurnFrame:
    """What one turn is, before its first iteration (R7a)."""

    turn_number: int
    task: TaskState
    #: What ``turn.start`` hooks injected (a volatile tail block; never persisted).
    hook_context: str = ""
    #: The turn clock, or ``None`` when the configuration sets no ceiling.
    deadline: Deadline | None = None


def _is_action(item: Any) -> bool:
    """A merge only counts as an action when it actually landed."""
    if item.call.name in ACTION_TOOLS:
        return True
    if item.call.name != "merge_agent":
        return False
    return (
        isinstance(item.result.data, dict)
        and item.result.data.get("status") == "merged"
    )


@dataclass
class TurnBooks:
    """The observable state of the turn so far."""

    #: The last iteration index the loop started (0 = nothing started yet).
    iterations: int = 0
    #: Every model step this turn made, across C3 continuation rounds.  The
    #: outcome and handoff report this one; ``iterations`` is round-local.
    total_iterations: int = 0
    #: Task-gate nudges injected (``[task-gate]``) — the R2 stop policy's budget.
    nudges: int = 0
    #: Tool calls asked for this turn, successful or not.
    attempted_calls: int = 0
    #: Tool calls that came back with ``success=True``.
    successful_calls: int = 0
    #: Consecutive iterations in which every attempted call failed (R6b tier 2).
    failed_streak: int = 0
    #: D: consecutive rounds where every call was neutral (plan_keep etc.) -- a
    #: neutral round is not "stuck", so the stall trigger must not fire on it.
    neutral_streak: int = 0
    #: Strategy nudges injected (``[strategy-gate]``).
    strategy_nudges: int = 0
    #: What failed, newest last — the handoff names the most recent one.
    failures: list[Failure] = field(default_factory=list)
    #: The latest thing the model said (``None`` when it said nothing at all).
    last_text: str | None = None
    #: Finish reason of the latest model step (``None`` before the first step).
    #: The loop reads it when closing the turn: only ``"length"`` is
    #: actionable (a bare stop is indistinguishable from done).
    last_finish_reason: str | None = None
    #: Summaries the compactor produced this turn (for the harvest to look at).
    summaries: list[str] = field(default_factory=list)
    #: Whether the tail's memory block has been reported this turn (R3 emits one
    #: ``MemoryInjected`` per turn, not one per iteration).
    memory_announced: bool = False
    #: Whether the tail's compaction event has been reported this turn (the
    #: ``ContextCompacted`` event fires once per turn, not once per iteration).
    compaction_announced: bool = False
    #: Lines the user typed while this turn was running and the loop absorbed
    #: (R7e-2).
    steering_messages: int = 0
    #: What the model asked its future self to remember about this turn (R8a).
    #: ``None`` = it never wrote one, and the harness line stands alone.
    checkpoint: str | None = None
    #: Successful calls that are evidence about the world (R8a, D-14).  It is what
    #: the harvest reads, so "the turn used tools" cannot be faked by writing a
    #: checkpoint: that is the turn talking about itself.
    evidence_calls: int = 0
    #: Per-step model latency, newest last (R0-4) — the reporter already emits
    #: each step's latency as an event; the books keep the series so the turn
    #: can account its wall-clock the way it accounts its iterations.
    step_latencies_ms: list[int] = field(default_factory=list)
    #: Model steps abandoned at the turn deadline (R0-4).  Their streamed
    #: deltas survive in the transcript; without this counter the books would
    #: claim those seconds never happened (measured: a 179s step vanishing).
    abandoned_steps: int = 0
    #: Latency of the latest model step, however it ended (R0-4).
    last_step_latency_ms: int = 0
    #: Tool names with at least one success this turn (R0-2): a later success
    #: is the evidence that closes an error question, so successes must be
    #: remembered by name, not just counted.
    tool_success: set[str] = field(default_factory=set)
    #: B1/B2: consecutive successful retrieval calls, and how many there were in
    #: total.  The streak is the only exactly-computable half of "is this turn
    #: learning anything" (docs/plan-m-b.md section 3).
    search_streak: int = 0
    search_calls: int = 0
    #: The last successful tool output's fingerprint and the number of
    #: consecutive successful calls that repeated it.  Unlike ``search_streak``
    #: this is not a retrieval-only measure: a successful ``find`` through bash
    #: is still a call that produced no new information.
    last_success_digest: str | None = None
    repeat_streak: int = 0
    #: Output digests already seen from successful workspace calls.  Keeping
    #: the set (not only the last one) is what catches a loop that alternates
    #: between several checks instead of issuing the same exact call.
    seen_success_outputs: set[str] = field(default_factory=set)
    #: I1/R-7: normalised evidence fingerprints already seen this turn (finding
    #: keys, not raw outputs -- see ``_finding_key``), and the accounting built
    #: on it: consecutive retrievals whose evidence was already seen, the
    #: non-empty ones among those, and how many findings were new.
    seen_findings: set[str] = field(default_factory=set)
    stale_streak: int = 0
    repeated_nonempty: int = 0
    novel_findings: int = 0
    #: I1/R-7 follow-up: line ranges already read from each path this turn.
    #: ``read`` keys include the output digest, so re-reading the *same file*
    #: with a slightly different window looked like a brand-new finding --
    #: measured: 11 reads of ``tasks.py`` in a row, no action, no nudge.  A
    #: contained/overlapping range is the same evidence, not news.
    read_coverage: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    #: B1: the harness ran the configured static checks for this turn.  Once per
    #: turn, because a re-fired L2 must not start mypy again.
    static_check_ran: bool = False
    #: The last emitted ledger signature: sorted ``(id, level)`` pairs.  The
    #: loop compares each round's open set against this to detect movement.
    ledger_signature: tuple[tuple[str, str], ...] = ()
    #: F1: skill loaded this turn (for outcome tracking). Empty = none loaded.
    loaded_skill: str = ""
    #: F2: Paris' Law fatigue index — accumulated error damage (Σ severity²).
    fatigue_index: float = 0.0
    #: F2: fatigue nudges already injected this turn.
    fatigue_nudges: int = 0
    #: A gate nudge was injected and the model has not run since.  A second
    #: injection would be advice the model never saw; it also turns any ladder
    #: bug that re-arms itself into an unbounded synthetic-message loop.
    nudge_pending: bool = False
    #: 13 §2.1: successful ``ask_user`` calls this turn — the helped signal of
    #: the ladder's ask levels (L3 for strategy/fatigue, L2 for task).
    ask_user_calls: int = 0
    #: 13 §2.5 (B): text deltas delivered this turn.  "The operator already saw
    #: part of the answer" is what earns a continuation instead of a failure.
    stream_deltas: int = 0
    #: 13 §2.5 (B): stream continuations spent this turn (bounded to 1).
    stream_continued: int = 0
    #: 13 §2.7 (H): retries spent on three-way-empty model steps (bounded to 1).
    empty_retries: int = 0

    # ---- notifications ----------------------------------------------------

    def note_iteration(self, index: int) -> None:
        """Record the iteration index about to start.

        ``iterations`` stays round-local (the C3 restart resets it), while
        ``total_iterations`` counts every model step this turn made — the
        number outcome and handoff report (H2 audit fix: a turn with three
        continuation rounds used to claim the last round's index).
        """
        self.iterations = index
        self.total_iterations += 1
        # The model is about to see the last injection; a gate may speak again
        # after this step.
        self.nudge_pending = False

    def note_nudge(self) -> None:
        """The task gate injected a reminder and will iterate again."""
        self.nudges += 1

    def note_fatigue_nudge(self) -> None:
        """The fatigue gate injected a reminder and will iterate again."""
        self.fatigue_nudges += 1

    def note_delta(self) -> None:
        """One text delta reached the caller (the stream-continuation signal)."""
        self.stream_deltas += 1

    def note_stream_continue(self) -> None:
        """The turn spent its one continuation on a broken stream (13 §2.5)."""
        self.stream_continued += 1

    def note_empty_retry(self) -> None:
        """The turn spent its one retry on an empty model step (13 §2.7)."""
        self.empty_retries += 1

    @property
    def ledger_open(self) -> int:
        """Open questions right now (length of the tracked signature)."""
        return len(self.ledger_signature)

    def ledger_items(self, *, stall_threshold: int) -> list[UncertaintyItem]:
        """Derive this round's open set from the recorded observations.

        Pure: the rules live in ``domain/ledger.py``; the books only supply
        the state.  The loop calls this once per round and emits when the
        signature moves.
        """
        return build_ledger(
            failures=self.failures,
            failed_streak=self.failed_streak,
            iteration=self.iterations,
            succeeded_tools=frozenset(self.tool_success),
            stall_threshold=stall_threshold,
        )

    def note_ledger(self, signature: Sequence[tuple[str, str]]) -> bool:
        """Track the open set's signature; return True when it moved.

        Emits are edge-triggered: the signature is what the emission triggers on.
        The ``LedgerUpdated`` event carries the item names, derived separately.
        """
        signature = tuple(signature)
        if signature == self.ledger_signature:
            return False
        self.ledger_signature = signature
        return True

    def note_strategy_nudge(self) -> None:
        """The escalation ladder spent its one "change strategy" nudge."""
        self.strategy_nudges += 1

    def note_steering(self, count: int = 1) -> None:
        """The user steered the turn: new instructions, so the streak restarts (Q5).

        The call counters are deliberately untouched: nothing succeeded, and the
        stall ladder is about tool progress, not about how many times we asked.
        """
        self.steering_messages += max(0, int(count))
        self.failed_streak = 0
        self.last_success_digest = None
        self.repeat_streak = 0
        self.seen_success_outputs.clear()
        # I1/R-7: the user changed the task, so what we saw may be irrelevant.
        self._clear_findings()

    def note_step(
        self,
        text: str | None,
        *,
        finish_reason: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        """Remember the latest model text (even when there is none)."""
        self.last_text = text
        self.last_finish_reason = finish_reason
        self.last_step_latency_ms = int(latency_ms)
        self.step_latencies_ms.append(int(latency_ms))

    def note_abandoned(self, latency_ms: int) -> None:
        """A model step cut by the turn clock: count its seconds (R0-4)."""
        self.abandoned_steps += 1
        self.last_step_latency_ms = int(latency_ms)
        self.step_latencies_ms.append(int(latency_ms))

    def note_checkpoint(self, text: str, *, limit: int = DEFAULT_NOTE_CHARS) -> None:
        """Record the model's own line for this turn (R8a, D-1/D-12).

        Last write wins: within one batch the calls run in the model's order, so
        the final line is the one it wanted to leave.  A blank body is a no-op -
        an empty write must never erase a real one.
        """
        body = one_line(text, limit)
        if body:
            self.checkpoint = body

    def note_outcomes(self, outcomes: Sequence[DispatchOutcome]) -> None:
        """Count a batch of tool outcomes: attempts, successes, streak, failures.

        A batch that is empty — the model planned without calling anything — is
        not evidence either way, so it neither raises nor resets the streak
        (that is what keeps the ``strict`` todo policy working, R6b).
        """
        if not outcomes:
            return
        self.attempted_calls += len(outcomes)
        succeeded = [item for item in outcomes if item.result.success]
        self.successful_calls += len(succeeded)
        self.tool_success.update(item.call.name for item in succeeded)
        # B1/B2: a successful look extends the streak; a successful *action*
        # (see ``ACTION_TOOLS``) ends it.  Everything else -- failures, state-only
        # tools, the goal/ask tools -- moves neither number: a failed call is
        # STRATEGY's evidence, and a todo write is not an action on the workspace,
        # so neither may be able to hide a stall from the ladder.
        for item in succeeded:
            if item.call.name in RETRIEVAL_TOOLS:
                self.search_streak += 1
                self.search_calls += 1
                # I1/R-7: a successful look also answers "is this news".
                self._note_finding(item)
            elif _is_action(item):
                self.search_streak = 0
                # The world may have moved: what we saw before is not evidence
                # about it any more.  A *failed* action changes nothing.
                self._clear_findings()
        # A successful workspace call whose output has already been seen this
        # turn yielded no new information.  The repeats need not be adjacent:
        # the measured loop alternates ``find``/``ls``/``wc`` variants, and a
        # strict "same as the previous call" rule never sees the loop.
        # Retrieval already has its own evidence counters (``search_streak`` /
        # ``stale_streak``); this tool-agnostic signal is for action tools,
        # where a command (``find`` through bash) can succeed without changing
        # anything.  Keeping reads out avoids double-arming SEARCH for the
        # ordinary "read two files with identical bodies" turn.
        if any(item.call.name == "update_plan" for item in succeeded):
            # "Plan updated" is plan bookkeeping, not repeated evidence.  It
            # must not keep the look-only repeat streak armed after the model
            # has actually advanced the plan.
            self.repeat_streak = 0
        action_calls = [
            item
            for item in succeeded
            if _is_action(item) and item.call.name != "update_plan"
        ]
        if action_calls:
            last = action_calls[-1]
            digest = _output_digest(last.result.output or "")
            if digest in self.seen_success_outputs:
                self.repeat_streak += 1
            else:
                self.seen_success_outputs.add(digest)
                self.repeat_streak = 0
                self.last_success_digest = digest
        # 13 §2.1: the ladder's ask helped-signal counts only what actually
        # reached the user (a denied call never did).
        self.ask_user_calls += sum(
            1 for item in succeeded if item.call.name == "ask_user"
        )
        self.evidence_calls += sum(
            1 for item in succeeded if item.call.name not in STATE_ONLY_TOOLS
        )
        # R8c D-11 + 20 §批次3: a call the *user* cancelled, or one whose
        # result is a plan-validation refusal, is not a failure to fix -- it
        # must not push the escalation ladder (two cancels would inject a
        # persisted strategy nudge, and the turn is supposed to carry on after
        # an Escape).  An all-neutral round now also resets the burn: the
        # strategy window closes "helped" exactly when the streak is 0, so
        # parking it here would read a working loop as a failing one.
        counted = [item for item in outcomes if item.result.error not in _NOT_EVIDENCE_ERRORS]
        if not counted:
            self.failed_streak = 0
            self.neutral_streak += 1
            return
        self.failed_streak = 0 if succeeded else self.failed_streak + 1
        self.neutral_streak = 0
        for item in counted:
            if not item.result.success:
                error = str(item.result.error or "failed")
                self.failures.append((
                    item.call.name,
                    error,
                    (item.result.output or "")[:_FAILURE_EXCERPT],
                ))
                # F2: incremental fatigue accumulation (Paris' Law: ΔD = C × s^m)
                severity = self._severity_of(error)
                self.fatigue_index += _FATIGUE_C * (severity ** _FATIGUE_M)

    # ---- I1/R-7: evidence fingerprints ----------------------------------

    def _clear_findings(self) -> None:
        """Forget the evidence seen so far (an action, or the user steering)."""
        self.seen_findings.clear()
        self.read_coverage.clear()
        self.stale_streak = 0
        self.repeated_nonempty = 0

    def _note_read_overlap(self, item: DispatchOutcome) -> bool:
        """True when this read only re-returns lines already seen this turn.

        Pagination is news (a new, disjoint range); reopening the same page --
        or a sub-range inside it -- is not.  Overlap is judged on the line
        numbers the tool printed, so no tool-specific state is needed.
        """
        params = item.call.params
        path = str(params.get("path", "") or "")
        if not path:
            return False
        numbers = [
            int(match.group(1))
            for line in (item.result.output or "").splitlines()
            if (match := _READ_NUMBERED.match(line))
        ]
        if not numbers:
            return False
        start, end = min(numbers), max(numbers)
        ranges = self.read_coverage.setdefault(path, [])
        for seen_start, seen_end in ranges:
            overlap = min(seen_end, end) - max(seen_start, start) + 1
            asked = max(1, end - start + 1)
            if overlap / asked >= 0.5:
                self.stale_streak += 1
                if _has_payload(item.result.output or ""):
                    self.repeated_nonempty += 1
                return True
        ranges.append((start, end))
        if len(ranges) > 32:
            ranges.pop(0)
        return False

    def _note_finding(self, item: DispatchOutcome) -> None:
        """Count one successful look as news or as the same old evidence."""
        name = item.call.name
        if name not in _FINDING_TOOLS:
            # memory/history/websearch embed the query; the subagent list is
            # harness bookkeeping.  Neutral: neither novel nor stale.
            return
        output = item.result.output or ""
        if name == "read" and self._note_read_overlap(item):
            return
        key = _finding_key(name, item.call.params, output)
        if key is None:
            return
        if key in self.seen_findings:
            self.stale_streak += 1
            if _has_payload(output):
                self.repeated_nonempty += 1
            return
        self.seen_findings.add(key)
        self.novel_findings += 1
        # News ends the old stale streak: what L3 measures is a *run* of repeats.
        self.stale_streak = 0
        self.repeated_nonempty = 0
        if len(self.seen_findings) > _EVIDENCE_LIMIT:
            # Clear, never evict: losing the whole set costs at most one false
            # negative; keeping most of it and evicting the wrong entry could
            # report a genuinely new finding as stale.
            self.seen_findings.clear()

    # ---- F2: Paris' Law fatigue ------------------------------------------

    @staticmethod
    def _severity_of(error: str) -> int:
        """Derive error severity from error text (1-5, 5 = most severe)."""
        error_lower = error.lower()
        for level in (5, 4, 3, 2):
            if any(kw in error_lower for kw in _SEVERITY_KEYWORDS.get(level, ())):
                return level
        return 1

    # ---- what a decision needs -------------------------------------------

    def snapshot(
        self,
        *,
        iteration: int,
        tokens_spent: int,
        open_items: Sequence[TodoItem] = (),
        final_text: str | None = None,
        continuations: int = 0,
    ) -> TurnSnapshot:
        """The one object :class:`TurnPolicy` decides from.

        Both call sites (may-an-iteration-start, and how-a-step-ended) use this,
        so the policy cannot be handed two different views of the same turn.
        """
        return TurnSnapshot(
            iteration=iteration,
            nudges=self.nudges,
            open_items=tuple(open_items),
            final_text=final_text,
            tokens_spent=tokens_spent,
            failed_streak=self.failed_streak,
            neutral_streak=self.neutral_streak,
            attempted_calls=self.attempted_calls,
            successful_calls=self.successful_calls,
            strategy_nudges=self.strategy_nudges,
            continuations=continuations,
            fatigue_index=self.fatigue_index,
            fatigue_nudges=self.fatigue_nudges,
            ask_user_calls=self.ask_user_calls,
            search_streak=self.search_streak,
            search_calls=self.search_calls,
            stale_streak=self.stale_streak,
            repeated_nonempty=self.repeated_nonempty,
            novel_findings=self.novel_findings,
            repeat_streak=self.repeat_streak,
        )
