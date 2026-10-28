"""Tool dispatch — the single gate for tool execution (R1a-3).

Every tool call the model asks for goes through :class:`ToolDispatcher`, which
is the only place that decides whether a call runs, asks the user, or is
refused — and the only place that turns all of that into a ``ToolResult`` the
model can reason about.

Design notes (docs/r1a-3-analysis.md §3.3):
  - consecutive ALLOW-tier ``parallel_safe`` calls form one concurrent group
    (dsh's runGroup); any other call is a barrier that runs alone, so approval
    prompts can never interleave and DENY/STOP abort cleanly at group edges;
  - an unknown tool name is never executed and never prompts (the tool list is
    ours, so an unknown name means the model hallucinated one);
  - refusals are structured (``error`` + ``data``), not just prose;
  - after a STOP the remaining calls still get a result, otherwise the declared
    tool_calls would have no matching tool messages and the next request 400s.

R2a adds a per-call budget: every execution is wrapped in a hard deadline
(``infra.deadline``), bounded by ``min(tool.max_duration_seconds, remaining turn
budget)``.  A call that runs out of budget comes back as a *structured*
``ToolResult`` (``error="tool_timeout"``) instead of hanging the turn, so the
declared tool_calls still pair up and the model can react (docs/r2-analysis.md §7.2).
"""

from __future__ import annotations

import asyncio
import enum
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

from ai_coding.core.events import (
    LoopObserver,
    NullObserver,
    ToolApprovalRequested,
    ToolFinished,
    ToolStarted,
    preview,
)
from ai_coding.core.guard import GuardChain, GuardVerdict, TurnView
from ai_coding.core.hooks import HookDecision, HookPoint
from ai_coding.domain.agents import PLAN_DIR
from ai_coding.domain.message import (
    CANCELLED_BY_USER,
    ToolCall,
    ToolCallRef,
    ToolResult,
    tool_call_from_ref,
)
from ai_coding.infra.deadline import (
    Deadline,
    DeadlineExpired,
    RunCancelled,
    remaining_of,
    run_with_deadline,
)
from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.security.credentials import credential_match
from ai_coding.security.permission_gate import (
    Decision,
    PermissionDecision,
    PermissionGate,
)
from ai_coding.security.permissions import posture_of
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


@dataclass(frozen=True)
class DispatchOutcome:
    """What happened to one requested call."""

    call: ToolCall
    result: ToolResult
    denied: bool = False  # this call did not run
    halted: bool = False  # the user asked to stop the whole turn


@dataclass(frozen=True)
class _Prepared:
    """A parsed call plus its registry lookup and permission verdict."""

    call: ToolCall
    tool: BaseTool | None
    decision: Any | None
    #: M3 T5: this ALLOW rests on a scoped rule (resource != "*"), so the
    #: read-only overlay must not veto it a second time.
    specific: bool = False
    # R4a: the loop's iteration, carried so the events can be correlated.
    iteration: int = 0


def _verdict(decision: Any) -> PermissionDecision:
    """Normalize a gate decision into the enum (unknown input fails closed)."""
    if isinstance(decision, Decision):
        return decision.decision
    if isinstance(decision, PermissionDecision):
        return decision
    try:
        return PermissionDecision(str(decision).lower())
    except ValueError:
        return PermissionDecision.WARN


def _reason(decision: Any) -> str:
    return str(getattr(decision, "reason", "") or "")


def _call_budget(tool: BaseTool, deadline: Deadline | None) -> float | None:
    """The tighter of the tool's own ceiling and what is left of the turn."""
    own = getattr(tool, "max_duration_seconds", None)
    remaining = deadline.remaining() if deadline is not None else None
    if own is None:
        return remaining
    if remaining is None:
        return float(own)
    return min(float(own), remaining)


def _authorized_name(tool: BaseTool | None, call: ToolCall) -> str:
    """What the *gate* judges for this call.

    One vessel can carry many calls: ``mcp_call`` delegates to ``fs__write_file``
    (M2), and the name has to be re-derived from the *current* params because a
    guard or hook rewrite may have changed which remote tool is about to run.
    """
    return call.name if tool is None else tool.permission_name(call.params)


def _plan_denial_reason(
    tool_name: str, reason: str, ctx: ToolContext
) -> str:
    """What the model reads when the plan posture refuses a file tool.

    A refusal that only says "denied by agent rule: edit *" tells the model
    what went wrong but not what works.  In the plan posture two writers are
    reachable for exactly one directory, so the refusal names that directory.
    Every other refusal keeps its rule's own words: the shell and the web
    tools are denied outright, and telling them about a plan directory would
    be a different lie.
    """
    if posture_of(ctx.mode, ctx.readonly) != "plan":
        return reason
    # The registry name, not the authorized name: an ``mcp_call`` vessel may
    # delegate to a remote "write", and a plan directory has nothing to say
    # about a remote tool.
    if tool_name.lower() not in ("write", "edit"):
        return reason
    workspace = str(getattr(ctx, "workspace", "") or "").rstrip("/")
    plan_dir = f"{workspace}/{PLAN_DIR}" if workspace else PLAN_DIR
    return (
        f"计划模式下只能修改计划目录内的文件：{plan_dir}/。"
        "工作区其他文件在计划模式下只读；请把计划正文或笔记写进该目录，"
        "或用 present_plan 提交计划。"
    )


def _ask_budget(deadline: Deadline | None, approval_timeout: float | None) -> float | None:
    """How long a human gets to answer: the turn clock, or the configured ceiling.

    R7c: whichever is tighter.  ``None`` means "wait as long as it takes", which is
    the default (a slow human is not a refusal) — but the moment the turn has a
    clock, the wait is inside it, so the clock can actually end the turn.
    """
    remaining = remaining_of(deadline)
    if approval_timeout is None or approval_timeout <= 0:
        return remaining
    if remaining is None:
        return float(approval_timeout)
    return min(float(approval_timeout), remaining)


class ApprovalVerdict(enum.Enum):
    """What came back from asking a human (R7c)."""

    APPROVED = "approved"
    DECLINED = "declined"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"


class ToolDispatcher:
    """Executes tool calls with permission, approval and bounded concurrency."""

    def __init__(
        self,
        registry: ToolRegistry,
        gate: PermissionGate,
        approval: ApprovalCallback | None = None,
        *,
        max_parallel: int = 4,
        observer: LoopObserver | None = None,
        guards: GuardChain | None = None,
        hooks: Any | None = None,
        approval_timeout: float | None = None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        self.registry = registry
        self.gate = gate
        self.approval = approval
        self.max_parallel = max(1, int(max_parallel))
        self.observer: LoopObserver = observer or NullObserver()
        # R4b: stateful policy that runs *after* the static gate and before the
        # user is bothered (docs/r4-analysis.md 3.2).
        self.guards = guards if guards is not None else GuardChain([])
        # R4c: user-supplied policy, run after the built-in guards.  May be a
        # Braid of runners (command + python); ``None`` means no hooks at all.
        self.hooks = hooks
        # R7c: how long a human gets to answer before the turn moves on without
        # them (``None``/0 = only the turn clock bounds the wait).
        self.approval_timeout = approval_timeout
        # R8c: the user's "drop this batch" switch (Esc).  ``None`` = no such key.
        self.cancel = cancel

    # ---- public API -------------------------------------------------------

    async def dispatch_batch(
        self,
        calls: list[ToolCallRef],
        ctx: ToolContext,
        *,
        deadline: Deadline | None = None,
        iteration: int = 0,
        view: TurnView | None = None,
        allow_replay: bool = False,
    ) -> list[DispatchOutcome]:
        """Run ``calls`` and return one outcome per call, in the given order.

        ``allow_replay`` is the write-ahead reuse switch, and it is **off for a
        live turn**.  It used to be always on, which meant a second identical
        ``bash`` in the same turn returned the *first* run's output without
        executing: eval-08 stage F had ten ``cat`` calls whose output and
        duration were byte-identical after the file had been edited, and the
        model concluded "the edit didn't apply".  Only a turn resumed after a
        crash may answer from the record -- that is the case M-C1 was written
        for, where the effect really did happen before the process died.
        """
        if not calls:
            return []

        prepared = [
            self._prepare(tool_call_from_ref(ref), ctx=ctx, iteration=iteration)
            for ref in calls
        ]

        outcomes: list[DispatchOutcome] = []
        halted = False
        for group in self._concurrent_groups(prepared):
            if halted:
                outcomes.extend(
                    DispatchOutcome(
                        call=item.call, result=self._skipped(item.call), denied=True
                    )
                    for item in group
                )
                continue
            if len(group) == 1:
                outcome = await self._handle(
                    group[0], ctx, deadline, view, allow_replay=allow_replay
                )
                outcomes.append(outcome)
                halted = outcome.halted
                continue
            semaphore = asyncio.Semaphore(self.max_parallel)

            async def run(item: _Prepared, gate: asyncio.Semaphore) -> DispatchOutcome:
                async with gate:
                    return await self._handle(
                        item, ctx, deadline, view, allow_replay=allow_replay
                    )

            ran = list(await asyncio.gather(*(run(item, semaphore) for item in group)))
            outcomes.extend(ran)
            # A group is ALLOW-tier and parallel-safe by construction, and the
            # stop verdict only exists on the WARN approval path -- nothing in
            # a group can halt today.  The check keeps the invariant honest if
            # that ever changes: the group drains, then the rest is skipped.
            halted = any(outcome.halted for outcome in ran)
        return outcomes

    # ---- preparation ------------------------------------------------------

    def _prepare(
        self, call: ToolCall, *, ctx: ToolContext, iteration: int = 0
    ) -> _Prepared:
        """Resolve the tool and its permission verdict exactly once per call."""
        tool = self.registry.get(call.name)
        decision = None if tool is None else self._check(tool, call, ctx)
        return _Prepared(
            call=call,
            tool=tool,
            decision=decision,
            specific=bool(getattr(decision, "specific", False)),
            iteration=iteration,
        )

    def _check(self, tool: BaseTool | None, call: ToolCall, ctx: ToolContext) -> Any:
        """Ask the gate with the two facts only this request knows.

        The session layer and the posture travel in the context: both are
        per-call facts (a rule added between turns, a mode flipped mid-turn),
        so neither belongs in the process-wide gate.
        """
        return self.gate.check(
            _authorized_name(tool, call),
            call.params,
            session_rules=ctx.permissions,
            agent_rules=ctx.agent_rules,
            posture=posture_of(ctx.mode, ctx.readonly),
        )

    def _concurrent_groups(self, prepared: list[_Prepared]) -> list[list[_Prepared]]:
        """Split the batch into concurrent runs and single-call barriers.

        Consecutive calls that are ALLOW-tier and ``parallel_safe`` form one
        group and run concurrently (bounded by ``max_parallel``); every other
        call is its own group.  Groups run in declared order — results stay in
        the model's order for free — and a WARN call's approval can never
        interleave with anything else, which is the guarantee the old
        batch-wide rule bought by serialising the whole batch.
        """
        groups: list[list[_Prepared]] = []
        last_concurrent = False
        for item in prepared:
            concurrent = (
                item.tool is not None
                and item.tool.parallel_safe
                and _verdict(item.decision) is PermissionDecision.ALLOW
            )
            if concurrent and last_concurrent:
                groups[-1].append(item)
            else:
                groups.append([item])
            last_concurrent = concurrent
        return groups

    # ---- one call ---------------------------------------------------------

    async def _handle(
        self,
        item: _Prepared,
        ctx: ToolContext,
        deadline: Deadline | None = None,
        view: TurnView | None = None,
        *,
        allow_replay: bool = False,
    ) -> DispatchOutcome:
        call = item.call
        # R4a: one context per *call*, so the execution can identify itself even
        # when the batch shares the rest of the context (traces need the call id).
        ctx = replace(ctx, call_id=call.id)
        self.observer.on_event(
            ToolStarted(
                call_id=call.id,
                tool_name=call.name,
                params=dict(call.params),
                iteration=item.iteration,
            )
        )
        started = time.monotonic()

        result, denied, halted = await self._decide(
            item, ctx, deadline, view, allow_replay=allow_replay
        )

        self.observer.on_event(
            ToolFinished(
                call_id=call.id,
                tool_name=call.name,
                success=result.success,
                output_preview=preview(result.output),
                duration_ms=int((time.monotonic() - started) * 1000),
                error=result.error,
                iteration=item.iteration,
            )
        )
        return DispatchOutcome(call=call, result=result, denied=denied, halted=halted)

    async def _decide(
        self,
        item: _Prepared,
        ctx: ToolContext,
        deadline: Deadline | None = None,
        view: TurnView | None = None,
        *,
        allow_replay: bool = False,
    ) -> tuple[ToolResult, bool, bool]:
        """Return ``(result, denied, halted)`` for one prepared call."""
        call = item.call

        if item.tool is None:
            return self._unknown(call), True, False

        verdict = _verdict(item.decision)
        reason = _reason(item.decision)

        if verdict is PermissionDecision.DENY:
            return self._denied(
                call, _plan_denial_reason(call.name, reason, ctx)
            ), True, False

        # 18 §10.1 / M2 T6: a read-only session refuses mutating calls
        # structurally.  It sits *after* the gate on purpose now: the gate
        # speaks first when it has something to say (the plan agent's rule
        # names what was refused and why), and this is the fallback for a
        # session whose posture is the human's own switch.  The refusal is
        # still before the approval, so no approval is ever spent on a call
        # the session refuses anyway.
        # M3 T5: in the plan posture a scoped allow is the plan directory
        # exception, and the overlay must not veto it.  Everywhere else the
        # overlay keeps its absolute meaning: ``/readonly`` is the human's
        # switch, and a session rule must not widen it.
        plan_exception = (
            posture_of(ctx.mode, ctx.readonly) == "plan"
            and item.specific
            and item.tool.mutates_world
        )
        if ctx.readonly and item.tool.mutates_world and not plan_exception:
            return self._readonly_denied(call), True, False

        # ---- A3/A5: guards, and a rewritten call must pass the gate again ----
        turn_view = view if view is not None else TurnView()
        guard_decision = self.guards.check(call, ctx, turn_view)
        rewritten_from: dict[str, Any] | None = None
        if guard_decision.verdict is GuardVerdict.DENY:
            return self._guard_denied(call, guard_decision), True, False
        if guard_decision.verdict is GuardVerdict.REWRITE:
            rewritten_from = dict(call.params)
            call = replace(call, params=dict(guard_decision.params or {}))
            item = replace(item, call=call, tool=item.tool)
            recheck = self._check(item.tool, call, ctx)
            if _verdict(recheck) is PermissionDecision.DENY:
                # A rewrite must never be a way around the static gate.
                return self._denied(call, _reason(recheck), rewritten=True), True, False
            if self.approval is None and _verdict(recheck) is PermissionDecision.WARN:
                # The rewritten form is *more* sensitive than the original; without
                # anyone to ask, refuse rather than silently run it.
                return self._not_approved(call, "rewritten call needs approval"), True, False

        # A4: user hooks may also block or rewrite ------------------------
        hook_rewriter = ""
        hook_decision = await self._run_hooks(
            HookPoint.TOOL_BEFORE,
            call,
            ctx,
            turn_view,
        )
        if hook_decision is not None and hook_decision.decision is HookDecision.BLOCK:
            return self._hook_blocked(call, hook_decision), True, False
        if hook_decision is not None and hook_decision.decision is HookDecision.REWRITE:
            hook_rewriter = hook_decision.hook
            if rewritten_from is None:
                rewritten_from = dict(call.params)
            call = replace(call, params=dict(hook_decision.params or {}))
            item = replace(item, call=call, tool=item.tool)
            recheck = self._check(item.tool, call, ctx)
            if _verdict(recheck) is PermissionDecision.DENY:
                return self._denied(call, _reason(recheck), rewritten=True), True, False

        tool = item.tool
        if tool is None:  # pragma: no cover - checked at the top of _decide
            return self._unknown(call), True, False

        # B: an outside-workspace path is a *question*, so it must not be
        # refused before the operator is asked.  Every other precheck reason
        # (an empty write, a stale read) is not the operator's to decide and
        # still short-circuits here, so those calls do not grow a prompt they
        # never had.  The distinction is the error code the tool reports, which
        # is why this checks the result instead of calling precheck twice.
        early = tool.precheck(call.params)
        if early is not None and early.error != "sandbox_violation":
            return early, True, False

        if verdict is PermissionDecision.WARN:
            if self.approval is None:
                return self._not_approved(call, "no approval callback"), True, False
            named = _authorized_name(tool, call)
            # P3 (补充式方案 06): make the question durable *before* anyone waits
            # on it, and clear it once answered.  The wait sits inside the tool
            # batch, ahead of the next checkpoint, so a crash during it used to
            # lose the pending call entirely -- the one real defect the snapshot
            # had.  The turn writes this to a marker file, never to the session
            # (a mid-batch session save would normalize the unpaired call away).
            if ctx.note_approval is not None:
                ctx.note_approval({
                    "callId": call.id,
                    "toolName": named,
                    "params": dict(call.params),
                    "reason": reason,
                })
            try:
                verdict_from_user = await self._ask(
                    call,
                    reason,
                    deadline,
                    named,
                    posture_of(ctx.mode, ctx.readonly),
                )
            finally:
                if ctx.note_approval is not None:
                    ctx.note_approval(None)
            if verdict_from_user is ApprovalVerdict.STOPPED:
                return self._stopped(call), True, True
            if verdict_from_user is ApprovalVerdict.TIMED_OUT:
                waited = _ask_budget(deadline, self.approval_timeout)
                return self._approval_timeout(call, waited), True, False
            if verdict_from_user is not ApprovalVerdict.APPROVED:
                return self._not_approved(call, "user declined"), True, False
            if (
                bool(getattr(self.approval, "is_human", False))
                and call.name.lower() in ("read", "grep")
                and credential_match(
                    str(call.params.get("path") or ""),
                    workspace=ctx.workspace,
                )
            ):
                # This approval belongs to this one credential call.  A
                # sibling call gets the dispatcher's original context, and an
                # automatic YES never reaches this branch.
                ctx = replace(ctx, credential_approved=True)

        # The call is allowed to run.  ``precheck`` decides whether it *can*,
        # and it can only know that now: the operator's answer is what may have
        # opened an outside path (B), and until it is recorded the sandbox still
        # refuses it.
        blocked = tool.precheck(call.params)
        if blocked is not None:
            return blocked, True, False

        # M-C1: claim before execution.  ``precheck`` has run (the params are
        # final) and ``_execute`` has not -- this is the one moment a
        # side-effecting call can be named without racing a concurrent sibling.
        # The digest is ``call_fingerprint(call)``, so it is stable across a
        # guard/hook rewrite above.
        #
        # Only a *resumed* turn may answer from the record (``allow_replay``):
        # in a live turn the same call must really run again, or the model reads
        # a stale result and the guards never see the repeat.
        log = getattr(ctx, "write_ahead", None)
        if tool.mutates_world and log is not None:
            reused = log.claim(call, allow_replay=allow_replay)
            if reused is not None:
                # The call *did* run -- before the crash.  Counting it now is
                # what keeps the duplicate/failed budgets honest across the
                # resume; the flag on the payload lets a trace tell a reuse from
                # a fresh run.
                data = dict(reused.data or {})
                data["replayed"] = True
                reused = replace(reused, data=data)
                result = self.guards.after(call, reused, ctx, turn_view)
                await self._run_hooks(
                    HookPoint.TOOL_AFTER, call, ctx, turn_view, result=result
                )
                return result, False, False

        result = await self._execute(tool, call, ctx, deadline)
        if rewritten_from is not None:
            result = self._annotate_rewrite(
                result, rewritten_from, guard_decision.guard or f"hook:{hook_rewriter}"
            )
        result = self.guards.after(call, result, ctx, turn_view)
        await self._run_hooks(HookPoint.TOOL_AFTER, call, ctx, turn_view, result=result)
        if tool.mutates_world and log is not None:
            log.settle(call, result)
        return result, result.error == "credential_approval_required", False

    async def _run_hooks(
        self,
        point: HookPoint,
        call: ToolCall,
        ctx: ToolContext,
        view: TurnView,
        *,
        result: ToolResult | None = None,
    ) -> Any | None:
        """Run user hooks for a point; return the first verdict that matters.

        Hooks are advisory by construction: their own runner already applies the
        capability-based failure semantics, so nothing here can raise into the
        dispatcher (docs/r4-analysis.md 6.3).
        """
        if self.hooks is None:
            return None
        payload: dict[str, Any] = {
            "tool": {"name": call.name, "params": dict(call.params)},
            "call_id": call.id,
            "iteration": view.iteration,
        }
        if result is not None:
            payload["result"] = {
                "success": result.success,
                "output_preview": result.output[:500],
                "error": result.error,
            }
        try:
            outcomes = await self.hooks.run(point, payload)
        except Exception:  # noqa: BLE001 — hook delivery must never break a call
            return None
        for outcome in outcomes:
            if outcome.decision is not HookDecision.ALLOW:
                return outcome
        return None

    async def _ask(
        self,
        call: ToolCall,
        reason: str,
        deadline: Deadline | None = None,
        auth_name: str | None = None,
        posture: str = "build",
    ) -> ApprovalVerdict:
        """Prompt the user, inside the turn clock.  Never raises (R7c).

        The wait used to be unbounded *and* unwrapped, so a prompt nobody answered
        held the turn for as long as it liked — measured: 5.00s against a 1.0s
        clock, and when the human finally answered, the call was refused because
        the clock had already passed (docs/r7-analysis.md 1.3).
        """
        # The prompt, the event and the grant all name the authorized call: an
        # operator asked to approve "mcp_call" cannot make an informed decision.
        named = auth_name or call.name
        self.observer.on_event(
            ToolApprovalRequested(
                tool_name=named, params=dict(call.params), reason=reason
            )
        )
        if self.approval is None:  # pragma: no cover - guarded by the caller
            return ApprovalVerdict.DECLINED
        try:
            answer = await run_with_deadline(
                self.approval.approve(named, call.params, reason),
                timeout=_ask_budget(deadline, self.approval_timeout),
                label="approval",
            )
        except DeadlineExpired:
            return ApprovalVerdict.TIMED_OUT
        except Exception:  # noqa: BLE001 — approval is a safety boundary: fail closed
            return ApprovalVerdict.DECLINED
        if answer is ApprovalResult.ALWAYS:
            # Recording is the *core's* job now, not the front end's: the REPL
            # and the TUI both arrive here, and a front end that forgot to
            # record would turn "always" into a silent "this once" -- exactly
            # how the REPL never had the feature at all (G4b).
            self.gate.grant(named, call.params, posture=posture)
            return ApprovalVerdict.APPROVED
        if answer is ApprovalResult.YES:
            # B: a one-off "yes" is still an answer, so the tool has to be able
            # to see it.  The sandbox asks the gate before refusing, and it did
            # not hear the answer -- this session's grant is what carries it
            # through to ``precheck``.  Session-scoped on purpose: it dies with
            # the process, where "always" is the one written to disk.
            allow = getattr(self.gate, "allow_once", None)
            if callable(allow):
                allow(named, call.params)
            return ApprovalVerdict.APPROVED
        if answer is ApprovalResult.STOP:
            return ApprovalVerdict.STOPPED
        return ApprovalVerdict.DECLINED

    async def _execute(
        self,
        tool: BaseTool,
        call: ToolCall,
        ctx: ToolContext,
        deadline: Deadline | None = None,
    ) -> ToolResult:
        if deadline is not None and deadline.expired:
            # Never start work we already know cannot finish.
            return self._tool_timeout(call, 0.0, "turn_budget_exhausted")

        budget = _call_budget(tool, deadline)
        # R8c: the tool may own a child process; the kill flag is how the cancel
        # reaches it (bash hands it to ``run_process``).
        kill = threading.Event()
        ctx = replace(ctx, kill_event=kill)
        try:
            return await run_with_deadline(
                tool.execute(call.params, ctx),
                timeout=budget,
                label=f"tool:{call.name}",
                cancel=self.cancel,
            )
        except RunCancelled:
            # The wait is over, but the work is not: set the flag so a subprocess
            # group is killed rather than left running (docs/r8-analysis.md S2.1).
            kill.set()
            return self._cancelled(call)
        except DeadlineExpired:
            return self._tool_timeout(
                call, budget if budget is not None else 0.0, "call_budget_exhausted"
            )
        except Exception as exc:  # noqa: BLE001 — a tool failure is an observation
            return ToolResult(
                success=False,
                output=f"error: tool '{call.name}' failed: {exc}",
                error="tool_error",
                data={"tool": call.name},
            )

    # ---- structured results ----------------------------------------------

    @staticmethod
    def _unknown(call: ToolCall) -> ToolResult:
        return ToolResult(
            success=False,
            output=f"error: unknown tool '{call.name}'",
            error="unknown_tool",
            data={"tool": call.name},
        )

    @staticmethod
    def _readonly_denied(call: ToolCall) -> ToolResult:
        """18 §10.1: a read-only session refuses mutations, and points at the
        tools that still work (内化批次: the old text taught the /plan family;
        the model -- and the transcript -- are steered to read/grep/glob and
        the review channel instead)."""
        return ToolResult(
            success=False,
            output=(
                "error: 只读已开启——该工具会改变工作区或状态，已拒绝。"
                "请改用 read/grep/glob 探索；计划完成后用 present_plan 提交评审。"
            ),
            error="readonly_denied",
            data={
                "decision": "deny",
                "reason": "read-only session",
                "readonly": True,
            },
        )

    @staticmethod
    def _denied(call: ToolCall, reason: str, *, rewritten: bool = False) -> ToolResult:
        detail = f": {reason}" if reason else ""
        prefix = "the rewritten call " if rewritten else ""
        return ToolResult(
            success=False,
            output=f"error: {prefix}被权限门拒绝{detail}",
            error="denied",
            data={
                "decision": "deny",
                "reason": reason,
                "tool": call.name,
                "rewritten": rewritten,
            },
        )

    @staticmethod
    def _guard_denied(call: ToolCall, decision: Any) -> ToolResult:
        """A guard refused this call (R4b); the reason is meant for the model."""
        return ToolResult(
            success=False,
            output=f"error: refused by runtime guard '{decision.guard}': {decision.reason}",
            error="guard_denied",
            data={
                "decision": "deny",
                "reason": decision.reason,
                "guard": decision.guard,
                "tool": call.name,
            },
        )

    @staticmethod
    def _hook_blocked(call: ToolCall, decision: Any) -> ToolResult:
        """A user hook refused this call (R4c)."""
        return ToolResult(
            success=False,
            output=f"error: refused by hook '{decision.hook}': {decision.reason}",
            error="hook_blocked",
            data={
                "decision": "deny",
                "reason": decision.reason,
                "hook": decision.hook,
                "tool": call.name,
            },
        )

    @staticmethod
    def _annotate_rewrite(
        result: ToolResult, original: dict[str, Any], guard: str
    ) -> ToolResult:
        """Say, in the result, that the arguments changed before execution.

        The persisted assistant message keeps the model's original call (that is
        what it asked for); without this note the transcript would read
        "requested X, got Y" with no explanation (docs/r4-analysis.md 5.3).
        """
        data = dict(result.data) if isinstance(result.data, dict) else {}
        data["rewritten_from"] = original
        data["rewritten_by"] = f"guard:{guard}" if guard else "guard"
        return replace(
            result,
            output=f"{result.output}\nnote: parameters were adjusted before execution",
            data=data,
        )

    @staticmethod
    def _not_approved(call: ToolCall, reason: str) -> ToolResult:
        return ToolResult(
            success=False,
            output="error: 用户未批准这次调用",
            error="not_approved",
            data={"decision": "warn", "approved": False, "reason": reason, "tool": call.name},
        )

    @staticmethod
    def _approval_timeout(call: ToolCall, seconds: float | None) -> ToolResult:
        """Nobody answered in time — a distinct state from "the user said no" (R7c).

        The model has to be able to tell the two apart: "they refused" means find
        another way, "they did not answer" means decide, say what you assumed, and
        carry on (which is what ``ask_user`` already does when there is no human).
        """
        waited = f" within {seconds:g}s" if seconds else ""
        return ToolResult(
            success=False,
            output=(
                f"error: nobody answered the approval prompt for '{call.name}'{waited}; "
                "the call was not run. Decide another way, say what you assumed, or stop."
            ),
            error="approval_timeout",
            data={
                "decision": "warn",
                "approved": False,
                "reason": "approval_timeout",
                "timeout_seconds": seconds,
                "tool": call.name,
            },
        )

    @staticmethod
    def _stopped(call: ToolCall) -> ToolResult:
        return ToolResult(
            success=False,
            output="error: 用户已停止",
            error="stopped",
            data={"decision": "warn", "stopped": True, "tool": call.name},
        )

    @staticmethod
    def _skipped(call: ToolCall) -> ToolResult:
        return ToolResult(
            success=False,
            output="error: 已跳过（本回合被用户停止）",
            error="skipped",
            data={"skipped": True, "tool": call.name},
        )

    @staticmethod
    def _tool_timeout(call: ToolCall, seconds: float, reason: str) -> ToolResult:
        """A budget ran out: structured, so the transcript stays valid."""
        return ToolResult(
            success=False,
            output=(
                f"error: tool '{call.name}' exceeded its {seconds:g}s budget; "
                "it was stopped and produced no result"
            ),
            error="tool_timeout",
            data={
                "tool": call.name,
                "timeout_seconds": seconds,
                "reason": reason,
            },
        )

    @staticmethod
    def _cancelled(call: ToolCall) -> ToolResult:
        """The user dropped this call (R8c): structured, and not an error to fix."""
        return ToolResult(
            success=False,
            output=(
                "cancelled_by_user: the user stopped this call. Do not repeat it "
                "unchanged - change approach, ask, or wrap the turn up."
            ),
            error=CANCELLED_BY_USER,
            data={"tool": call.name},
        )


__all__ = ["ToolDispatcher", "DispatchOutcome"]
