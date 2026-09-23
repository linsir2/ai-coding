"""Tool dispatch — the single gate for tool execution (R1a-3).

Every tool call the model asks for goes through :class:`ToolDispatcher`, which
is the only place that decides whether a call runs, asks the user, or is
refused — and the only place that turns all of that into a ``ToolResult`` the
model can reason about.

Design notes (docs/r1a-3-analysis.md §3.3):
  - a batch runs concurrently only when **every** call is ALLOW tier *and*
    ``parallel_safe``; otherwise the whole batch is sequential, so approval
    prompts can never interleave and DENY/STOP abort cleanly;
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
import time
from dataclasses import dataclass
from typing import Any

from ai_coding.core.events import (
    LoopObserver,
    NullObserver,
    ToolApprovalRequested,
    ToolFinished,
    ToolStarted,
    preview,
)
from ai_coding.domain.message import ToolCall, ToolCallRef, ToolResult, tool_call_from_ref
from ai_coding.infra.deadline import Deadline, DeadlineExpired, run_with_deadline
from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.security.permission_gate import (
    Decision,
    PermissionDecision,
    PermissionGate,
)
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
    ) -> None:
        self.registry = registry
        self.gate = gate
        self.approval = approval
        self.max_parallel = max(1, int(max_parallel))
        self.observer: LoopObserver = observer or NullObserver()

    # ---- public API -------------------------------------------------------

    async def dispatch_batch(
        self,
        calls: list[ToolCallRef],
        ctx: ToolContext,
        *,
        deadline: Deadline | None = None,
    ) -> list[DispatchOutcome]:
        """Run ``calls`` and return one outcome per call, in the given order."""
        if not calls:
            return []

        prepared = [self._prepare(tool_call_from_ref(ref)) for ref in calls]

        if self._can_run_parallel(prepared):
            semaphore = asyncio.Semaphore(self.max_parallel)

            async def run(item: _Prepared) -> DispatchOutcome:
                async with semaphore:
                    return await self._handle(item, ctx, deadline)

            return list(await asyncio.gather(*(run(item) for item in prepared)))

        outcomes: list[DispatchOutcome] = []
        halted = False
        for item in prepared:
            if halted:
                outcomes.append(
                    DispatchOutcome(
                        call=item.call, result=self._skipped(item.call), denied=True
                    )
                )
                continue
            outcome = await self._handle(item, ctx, deadline)
            outcomes.append(outcome)
            halted = outcome.halted
        return outcomes

    # ---- preparation ------------------------------------------------------

    def _prepare(self, call: ToolCall) -> _Prepared:
        """Resolve the tool and its permission verdict exactly once per call."""
        tool = self.registry.get(call.name)
        decision = None if tool is None else self.gate.check(call.name, call.params)
        return _Prepared(call=call, tool=tool, decision=decision)

    def _can_run_parallel(self, prepared: list[_Prepared]) -> bool:
        """Concurrency requires *every* call to be an ALLOW-tier safe tool."""
        for item in prepared:
            if item.tool is None or not item.tool.parallel_safe:
                return False
            if _verdict(item.decision) is not PermissionDecision.ALLOW:
                return False
        return True

    # ---- one call ---------------------------------------------------------

    async def _handle(
        self,
        item: _Prepared,
        ctx: ToolContext,
        deadline: Deadline | None = None,
    ) -> DispatchOutcome:
        call = item.call
        self.observer.on_event(
            ToolStarted(call_id=call.id, tool_name=call.name, params=dict(call.params))
        )
        started = time.monotonic()

        result, denied, halted = await self._decide(item, ctx, deadline)

        self.observer.on_event(
            ToolFinished(
                call_id=call.id,
                tool_name=call.name,
                success=result.success,
                output_preview=preview(result.output),
                duration_ms=int((time.monotonic() - started) * 1000),
                error=result.error,
            )
        )
        return DispatchOutcome(call=call, result=result, denied=denied, halted=halted)

    async def _decide(
        self,
        item: _Prepared,
        ctx: ToolContext,
        deadline: Deadline | None = None,
    ) -> tuple[ToolResult, bool, bool]:
        """Return ``(result, denied, halted)`` for one prepared call."""
        call = item.call

        if item.tool is None:
            return self._unknown(call), True, False

        verdict = _verdict(item.decision)
        reason = _reason(item.decision)

        if verdict is PermissionDecision.DENY:
            return self._denied(call, reason), True, False

        if verdict is PermissionDecision.WARN:
            if self.approval is None:
                return self._not_approved(call, "no approval callback"), True, False
            approved, stopped = await self._ask(call, reason)
            if stopped:
                return self._stopped(call), True, True
            if not approved:
                return self._not_approved(call, "user declined"), True, False

        return await self._execute(item.tool, call, ctx, deadline), False, False

    async def _ask(self, call: ToolCall, reason: str) -> tuple[bool, bool]:
        """Prompt the user.  Returns ``(approved, stopped)``; never raises."""
        self.observer.on_event(
            ToolApprovalRequested(
                tool_name=call.name, params=dict(call.params), reason=reason
            )
        )
        if self.approval is None:  # pragma: no cover - guarded by the caller
            return False, False
        try:
            answer = await self.approval.approve(call.name, call.params, reason)
        except Exception:  # noqa: BLE001 — approval is a safety boundary: fail closed
            return False, False
        if answer is ApprovalResult.YES:
            return True, False
        if answer is ApprovalResult.STOP:
            return False, True
        return False, False

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
        try:
            return await run_with_deadline(
                tool.execute(call.params, ctx),
                timeout=budget,
                label=f"tool:{call.name}",
            )
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
    def _denied(call: ToolCall, reason: str) -> ToolResult:
        detail = f": {reason}" if reason else ""
        return ToolResult(
            success=False,
            output=f"error: denied by permission gate{detail}",
            error="denied",
            data={"decision": "deny", "reason": reason, "tool": call.name},
        )

    @staticmethod
    def _not_approved(call: ToolCall, reason: str) -> ToolResult:
        return ToolResult(
            success=False,
            output="error: not approved by user",
            error="not_approved",
            data={"decision": "warn", "approved": False, "reason": reason, "tool": call.name},
        )

    @staticmethod
    def _stopped(call: ToolCall) -> ToolResult:
        return ToolResult(
            success=False,
            output="error: stopped by user",
            error="stopped",
            data={"decision": "warn", "stopped": True, "tool": call.name},
        )

    @staticmethod
    def _skipped(call: ToolCall) -> ToolResult:
        return ToolResult(
            success=False,
            output="error: skipped (turn stopped by user)",
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


__all__ = ["ToolDispatcher", "DispatchOutcome"]
