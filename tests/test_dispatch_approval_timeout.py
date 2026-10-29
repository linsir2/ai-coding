"""R7c: an approval that nobody answers must not hold the turn forever.

Measured before the fix (docs/r7-analysis.md 1.3): with a 1.0s turn clock and an
approval that takes 5s to answer, the turn took **5.00s** — and the second bug was
worse than the delay.  When the answer finally arrived, the call was then refused
with ``tool_timeout`` because the clock had already passed: *the human answered and
the work was thrown away.*

Threading the callback alone was not enough (5.00s again): the wait itself needs a
cancellation point, so ``_ask`` is bounded by whichever comes first — the rest of
the turn clock, or ``approval_timeout_seconds`` when one is configured.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.core.events import RecordingObserver, ToolFinished
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.infra.deadline import Deadline
from ai_coding.security.approval import (
    ApprovalCallback,
    ApprovalResult,
    CLIApprovalCallback,
)
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


class _Bash(BaseTool):
    """A WARN-tier tool (the gate asks before running it)."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "bash"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="ran")


class _SlowApproval(ApprovalCallback):
    """A human who takes ``seconds`` to answer."""

    def __init__(self, seconds: float, answer: ApprovalResult = ApprovalResult.YES) -> None:
        self.seconds = seconds
        self.answer = answer
        self.asked = 0

    async def approve(self, tool_name, params, reason):
        self.asked += 1
        await asyncio.sleep(self.seconds)
        return self.answer


def _dispatcher(approval: Any, **kwargs: Any) -> ToolDispatcher:
    registry = ToolRegistry()
    registry.register(_Bash())
    return ToolDispatcher(registry, PermissionGate(), approval, max_parallel=1, **kwargs)


_CALL = [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')]


async def test_threaded_approval_is_bounded_by_the_turn_clock():
    dispatcher = _dispatcher(_SlowApproval(5.0))
    started = time.monotonic()
    outcomes = await dispatcher.dispatch_batch(
        _CALL, ToolContext(workspace="."), deadline=Deadline.after(1.0)
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert outcomes[0].result.error == "approval_timeout"


async def test_an_expired_approval_says_so_and_is_not_a_user_refusal():
    dispatcher = _dispatcher(_SlowApproval(5.0))
    outcomes = await dispatcher.dispatch_batch(
        _CALL, ToolContext(workspace="."), deadline=Deadline.after(1.0)
    )
    result = outcomes[0].result
    assert result.error == "approval_timeout"  # not "not_approved": the human never spoke
    assert result.data is not None
    assert result.data["approved"] is False
    assert result.data["reason"] == "approval_timeout"
    assert "declin" not in result.output


async def test_an_expired_approval_does_not_halt_the_turn():
    dispatcher = _dispatcher(_SlowApproval(5.0))
    outcomes = await dispatcher.dispatch_batch(
        _CALL, ToolContext(workspace="."), deadline=Deadline.after(1.0)
    )
    assert outcomes[0].halted is False
    assert outcomes[0].denied is True


async def test_a10_a_timed_out_wait_is_reported_even_though_the_call_never_ran():
    observer = RecordingObserver()
    dispatcher = _dispatcher(_SlowApproval(5.0), approval_timeout=0.5, observer=observer)
    await dispatcher.dispatch_batch(_CALL, ToolContext(workspace="."))
    finished = observer.of_type(ToolFinished)[0]
    assert finished.approval_ms >= 400
    assert finished.duration_ms >= finished.approval_ms


async def test_a10_a_timed_out_prompt_names_the_budget_it_was_given():
    """The message must quote 0.5s, not the ~0 left after the wait."""
    dispatcher = _dispatcher(_SlowApproval(5.0), approval_timeout=0.5)
    outcomes = await dispatcher.dispatch_batch(_CALL, ToolContext(workspace="."))
    assert "within 0.5s" in outcomes[0].result.output


async def test_a_configured_approval_timeout_bounds_a_turn_without_a_clock():
    dispatcher = _dispatcher(_SlowApproval(5.0), approval_timeout=0.5)
    started = time.monotonic()
    outcomes = await dispatcher.dispatch_batch(_CALL, ToolContext(workspace="."))
    assert time.monotonic() - started < 2.0
    assert outcomes[0].result.error == "approval_timeout"


async def test_no_configured_timeout_and_no_clock_still_waits_for_the_human():
    """The default must not turn a slow human into a refusal."""
    approval = _SlowApproval(0.2)
    dispatcher = _dispatcher(approval)
    outcomes = await dispatcher.dispatch_batch(_CALL, ToolContext(workspace="."))
    assert approval.asked == 1
    assert outcomes[0].result.success is True


async def test_an_answered_approval_is_used_as_before():
    dispatcher = _dispatcher(_SlowApproval(0.05, ApprovalResult.NO))
    outcomes = await dispatcher.dispatch_batch(
        _CALL, ToolContext(workspace="."), deadline=Deadline.after(5.0)
    )
    assert outcomes[0].result.error == "not_approved"  # a real refusal, not a timeout


async def test_a_stopping_answer_still_stops_the_turn():
    dispatcher = _dispatcher(_SlowApproval(0.05, ApprovalResult.STOP))
    outcomes = await dispatcher.dispatch_batch(
        _CALL, ToolContext(workspace="."), deadline=Deadline.after(5.0)
    )
    assert outcomes[0].halted is True
    assert outcomes[0].result.error == "stopped"


async def test_the_synchronous_cli_callback_no_longer_freezes_the_loop():
    """The R7c regression: `input()` used to block the event loop thread itself."""

    def sleepy_input(prompt: str) -> str:
        time.sleep(1.5)
        return "y"

    dispatcher = _dispatcher(CLIApprovalCallback(input_fn=sleepy_input))
    started = time.monotonic()
    outcomes = await dispatcher.dispatch_batch(
        _CALL, ToolContext(workspace="."), deadline=Deadline.after(0.5)
    )
    assert time.monotonic() - started < 1.0
    assert outcomes[0].result.error == "approval_timeout"
