"""TDD (R1a-3): ToolDispatcher — the single gate for tool execution.

The dispatcher is where permission, approval, concurrency and structured denial
all meet, so every branch gets its own test.  Tool names here are chosen to hit
the *real* ``PermissionGate`` tiers: read/glob are ALLOW, write/edit/bash are WARN,
and bash with a hard-deny pattern is DENY.
"""

import asyncio
import dataclasses
import time

import pytest

from ai_coding.core.dispatch import DispatchOutcome, ToolDispatcher
from ai_coding.core.events import (
    RecordingObserver,
    ToolApprovalRequested,
    ToolFinished,
    ToolStarted,
)
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.task import TaskState
from ai_coding.infra.deadline import Deadline
from ai_coding.security.approval import ApprovalResult
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _FakeTool(BaseTool):
    """A tool that records how it was called and can misbehave on demand."""

    def __init__(
        self,
        name: str,
        *,
        parallel_safe: bool = False,
        result: ToolResult | None = None,
        raiser: Exception | None = None,
        on_execute=None,
    ) -> None:
        self._name = name
        self.parallel_safe = parallel_safe
        self._result = result or ToolResult(success=True, output="ok")
        self._raiser = raiser
        self._on_execute = on_execute
        self.params_seen: list[dict] = []
        self.contexts_seen: list[ToolContext | None] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake tool"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.params_seen.append(params)
        self.contexts_seen.append(ctx)
        if self._on_execute is not None:
            await self._on_execute(params)
        if self._raiser is not None:
            raise self._raiser
        return self._result

    @property
    def executed(self) -> bool:
        return bool(self.params_seen)


class _Approval:
    """Records prompts and answers with a fixed ApprovalResult."""

    def __init__(self, answer: ApprovalResult | None = None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict, str]] = []

    async def approve(self, tool_name, params, reason):
        self.calls.append((tool_name, params, reason))
        if self.answer is None:
            raise RuntimeError("approval backend exploded")
        return self.answer


class _NeverAsked(_Approval):
    async def approve(self, tool_name, params, reason):
        raise AssertionError("approval must not be requested for this call")


class _ConcurrencyProbe:
    """Tracks the highest number of overlapping executions."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    async def enter(self, _params=None) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.02)
        self.current -= 1


def _ref(call_id: str, name: str, arguments: str = "{}") -> ToolCallRef:
    return ToolCallRef(id=call_id, name=name, arguments=arguments)


def _ctx() -> ToolContext:
    return ToolContext(task=TaskState(goal="g"), workspace="/tmp/ws", session_id="sid")


def _dispatcher(tools, *, approval=None, max_parallel=4, observer=None):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolDispatcher(
        registry,
        PermissionGate(),
        approval,
        max_parallel=max_parallel,
        observer=observer,
    )


# ---- permission tiers ------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_tool_executes_directly():
    tool = _FakeTool("read")
    outcomes = await _dispatcher([tool]).dispatch_batch([_ref("c1", "read")], _ctx())
    assert len(outcomes) == 1
    assert outcomes[0].result.success is True
    assert outcomes[0].denied is False
    assert tool.executed is True


@pytest.mark.asyncio
async def test_warn_tool_approval_yes_executes():
    tool = _FakeTool("write")
    approval = _Approval(ApprovalResult.YES)
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write", '{"path":"a"}')], _ctx()
    )
    assert outcomes[0].result.success is True
    assert tool.executed is True
    assert approval.calls[0][0] == "write"


@pytest.mark.asyncio
async def test_warn_tool_approval_no_returns_not_approved():
    tool = _FakeTool("write")
    approval = _Approval(ApprovalResult.NO)
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write")], _ctx()
    )
    result = outcomes[0].result
    assert result.success is False
    assert result.error == "not_approved"
    assert tool.executed is False
    assert outcomes[0].denied is True


@pytest.mark.asyncio
async def test_warn_tool_without_callback_is_denied():
    tool = _FakeTool("write")
    outcomes = await _dispatcher([tool], approval=None).dispatch_batch(
        [_ref("c1", "write")], _ctx()
    )
    result = outcomes[0].result
    assert result.success is False
    assert result.error == "not_approved"
    assert result.data is not None
    assert result.data["reason"] == "no approval callback"
    assert tool.executed is False


@pytest.mark.asyncio
async def test_deny_tool_never_executes():
    tool = _FakeTool("bash")
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "bash", '{"command":"rm -rf /"}')], _ctx()
    )
    assert outcomes[0].result.success is False
    assert outcomes[0].result.error == "denied"
    assert tool.executed is False
    assert outcomes[0].denied is True


@pytest.mark.asyncio
async def test_deny_result_carries_reason():
    tool = _FakeTool("bash")
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "bash", '{"command":"sudo rm something"}')], _ctx()
    )
    data = outcomes[0].result.data
    assert data is not None
    assert data["decision"] == "deny"
    assert data["reason"]


@pytest.mark.asyncio
async def test_deny_does_not_ask_approval():
    tool = _FakeTool("bash")
    approval = _NeverAsked()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "bash", '{"command":"reboot"}')], _ctx()
    )
    assert outcomes[0].result.error == "denied"
    assert approval.calls == []


@pytest.mark.asyncio
async def test_approval_callback_exception_is_fail_closed():
    tool = _FakeTool("write")
    approval = _Approval(None)  # raises inside approve()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write")], _ctx()
    )
    assert outcomes[0].result.success is False
    assert outcomes[0].result.error == "not_approved"
    assert tool.executed is False


# ---- unknown tools ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_result():
    outcomes = await _dispatcher([_FakeTool("read")]).dispatch_batch(
        [_ref("c1", "nope")], _ctx()
    )
    result = outcomes[0].result
    assert result.success is False
    assert result.error == "unknown_tool"
    assert "nope" in result.output


@pytest.mark.asyncio
async def test_unknown_tool_does_not_ask_approval():
    approval = _NeverAsked()
    outcomes = await _dispatcher([_FakeTool("read")], approval=approval).dispatch_batch(
        [_ref("c1", "hallucinated")], _ctx()
    )
    assert outcomes[0].result.error == "unknown_tool"
    assert approval.calls == []


# ---- argument parsing / failures ------------------------------------------


@pytest.mark.asyncio
async def test_malformed_arguments_degrade_to_empty_params():
    tool = _FakeTool("read")
    await _dispatcher([tool]).dispatch_batch([_ref("c1", "read", "{not json")], _ctx())
    assert tool.params_seen == [{}]


@pytest.mark.asyncio
async def test_tool_exception_becomes_failed_result():
    tool = _FakeTool("read", raiser=RuntimeError("disk on fire"))
    outcomes = await _dispatcher([tool]).dispatch_batch([_ref("c1", "read")], _ctx())
    result = outcomes[0].result
    assert result.success is False
    assert result.error == "tool_error"
    assert "disk on fire" in result.output


@pytest.mark.asyncio
async def test_tool_receives_the_given_context():
    tool = _FakeTool("read")
    ctx = _ctx()
    await _dispatcher([tool]).dispatch_batch([_ref("c1", "read")], ctx)
    # R4a contract change: the dispatcher stamps a per-call ``call_id`` onto a copy
    # of the batch context, so a concurrent batch can still attribute each
    # execution (docs/r4-analysis.md 4.6).  Everything else is passed through.
    received = tool.contexts_seen[0]
    assert received.call_id == "c1"
    assert dataclasses.replace(received, call_id="") == ctx


# ---- STOP / skipped --------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_stop_halts_batch():
    writer = _FakeTool("write")
    reader = _FakeTool("read")
    approval = _Approval(ApprovalResult.STOP)
    outcomes = await _dispatcher([writer, reader], approval=approval).dispatch_batch(
        [_ref("c1", "write"), _ref("c2", "read")], _ctx()
    )
    assert outcomes[0].halted is True
    assert outcomes[0].result.error == "stopped"
    assert outcomes[1].halted is False
    assert reader.executed is False


@pytest.mark.asyncio
async def test_stopped_batch_marks_remaining_calls_skipped():
    """Every declared call needs a tool message or the next request 400s."""
    writer = _FakeTool("write")
    reader = _FakeTool("read")
    approval = _Approval(ApprovalResult.STOP)
    outcomes = await _dispatcher([writer, reader], approval=approval).dispatch_batch(
        [_ref("c1", "write"), _ref("c2", "read"), _ref("c3", "read")], _ctx()
    )
    for outcome in outcomes[1:]:
        assert outcome.result.success is False
        assert outcome.result.error == "skipped"
        assert outcome.result.data is not None
        assert outcome.result.data["skipped"] is True


# ---- ordering / batching ---------------------------------------------------


@pytest.mark.asyncio
async def test_empty_batch_returns_empty():
    assert await _dispatcher([_FakeTool("read")]).dispatch_batch([], _ctx()) == []


@pytest.mark.asyncio
async def test_outcomes_match_input_order():
    tools = [_FakeTool("write") for _ in range(3)]
    registry = ToolRegistry()
    for i, tool in enumerate(tools):
        tool._name = f"write{i}"
        registry.register(tool)

    class _Gate(PermissionGate):
        def check(self, tool_name, params):
            from ai_coding.security.permission_gate import Decision, PermissionDecision

            return Decision(PermissionDecision.ALLOW, "")

    dispatcher = ToolDispatcher(registry, _Gate(), _Approval(ApprovalResult.YES))
    outcomes = await dispatcher.dispatch_batch(
        [_ref("c1", "write0"), _ref("c2", "write1"), _ref("c3", "write2")], _ctx()
    )
    assert [o.call.id for o in outcomes] == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_parallel_safe_batch_runs_concurrently():
    probe = _ConcurrencyProbe()
    a = _FakeTool("read", parallel_safe=True, on_execute=probe.enter)
    b = _FakeTool("glob", parallel_safe=True, on_execute=probe.enter)
    await _dispatcher([a, b]).dispatch_batch(
        [_ref("c1", "read"), _ref("c2", "glob")], _ctx()
    )
    assert probe.peak == 2


@pytest.mark.asyncio
async def test_batch_with_one_unsafe_tool_goes_serial():
    """read + glob are both ALLOW, but one opts out of concurrency."""
    probe = _ConcurrencyProbe()
    a = _FakeTool("read", parallel_safe=True, on_execute=probe.enter)
    b = _FakeTool("glob", parallel_safe=False, on_execute=probe.enter)
    await _dispatcher([a, b]).dispatch_batch(
        [_ref("c1", "read"), _ref("c2", "glob")], _ctx()
    )
    assert probe.peak == 1


@pytest.mark.asyncio
async def test_batch_containing_warn_tool_runs_serial():
    """A WARN tier in the batch forbids concurrency: prompts must not interleave."""
    probe = _ConcurrencyProbe()
    a = _FakeTool("read", parallel_safe=True, on_execute=probe.enter)
    b = _FakeTool("write", parallel_safe=True, on_execute=probe.enter)
    await _dispatcher([a, b], approval=_Approval(ApprovalResult.YES)).dispatch_batch(
        [_ref("c1", "read"), _ref("c2", "write")], _ctx()
    )
    assert probe.peak == 1


@pytest.mark.asyncio
async def test_parallelism_bounded_by_max_parallel():
    probe = _ConcurrencyProbe()
    tools = [
        _FakeTool("read", parallel_safe=True, on_execute=probe.enter) for _ in range(3)
    ]
    registry = ToolRegistry()
    for i, tool in enumerate(tools):
        tool._name = f"read{i}"
        registry.register(tool)

    class _AllowAll(PermissionGate):
        def check(self, tool_name, params):
            from ai_coding.security.permission_gate import Decision, PermissionDecision

            return Decision(PermissionDecision.ALLOW, "")

    dispatcher = ToolDispatcher(registry, _AllowAll(), max_parallel=1)
    await dispatcher.dispatch_batch(
        [_ref("c1", "read0"), _ref("c2", "read1"), _ref("c3", "read2")], _ctx()
    )
    assert probe.peak == 1


# ---- events ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_emits_tool_started_and_finished():
    observer = RecordingObserver()
    tool = _FakeTool("read")
    await _dispatcher([tool], observer=observer).dispatch_batch(
        [_ref("c1", "read")], _ctx()
    )
    started = observer.of_type(ToolStarted)
    finished = observer.of_type(ToolFinished)
    assert len(started) == 1 and started[0].call_id == "c1"
    assert len(finished) == 1 and finished[0].success is True


@pytest.mark.asyncio
async def test_approval_request_event_emitted():
    observer = RecordingObserver()
    tool = _FakeTool("write")
    dispatcher = _dispatcher(
        [tool], approval=_Approval(ApprovalResult.YES), observer=observer
    )
    await dispatcher.dispatch_batch([_ref("c1", "write")], _ctx())
    requested = observer.of_type(ToolApprovalRequested)
    assert len(requested) == 1
    assert requested[0].tool_name == "write"


@pytest.mark.asyncio
async def test_denied_call_still_reports_finished_event():
    observer = RecordingObserver()
    tool = _FakeTool("bash")
    await _dispatcher([tool], observer=observer).dispatch_batch(
        [_ref("c1", "bash", '{"command":"mkfs.ext4 /dev/sda"}')], _ctx()
    )
    finished = observer.of_type(ToolFinished)
    assert len(finished) == 1
    assert finished[0].success is False


@pytest.mark.asyncio
async def test_output_preview_truncated_in_event():
    observer = RecordingObserver()
    tool = _FakeTool("read", result=ToolResult(success=True, output="y" * 5000))
    await _dispatcher([tool], observer=observer).dispatch_batch(
        [_ref("c1", "read")], _ctx()
    )
    preview_text = observer.of_type(ToolFinished)[0].output_preview
    assert len(preview_text) <= 501
    assert preview_text.endswith("…")


def test_dispatch_outcome_defaults():
    from ai_coding.domain.message import ToolCall

    outcome = DispatchOutcome(
        call=ToolCall(id="c1", name="read", params={}), result=ToolResult(success=True)
    )
    assert outcome.denied is False
    assert outcome.halted is False


# ---- R2a: per-call budgets ---------------------------------------------
# A call must be bounded even when the tool itself has no timeout, and a stuck
# call must come back as a *structured* result so the transcript stays valid and
# the model can react (docs/r2-analysis.md section 7.2).


class _BudgetedTool(_FakeTool):
    """Declares its own per-call ceiling, and can hang like ``communicate()``."""

    def __init__(self, name: str, *, seconds: float | None, hang: bool = False, **kwargs):
        super().__init__(name, **kwargs)
        self._seconds = seconds
        self._hang = hang

    @property
    def max_duration_seconds(self) -> float | None:
        return self._seconds

    async def execute(self, params, ctx=None):  # type: ignore[override]
        if not self._hang:
            return await super().execute(params, ctx)
        self.params_seen.append(params)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise
        return ToolResult(success=True, output="never")


@pytest.mark.asyncio
async def test_call_timeout_returns_structured_tool_timeout():
    tool = _BudgetedTool("read", seconds=0.05, hang=True)
    outcomes = await _dispatcher([tool]).dispatch_batch([_ref("t1", "read")], _ctx())

    result = outcomes[0].result
    assert outcomes[0].denied is False
    assert result.success is False
    assert result.error == "tool_timeout"
    assert result.data is not None
    assert result.data["tool"] == "read"
    assert result.data["timeout_seconds"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_expired_deadline_skips_execution():
    tool = _FakeTool("read")
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("t1", "read")], _ctx(), deadline=Deadline.after(-1)
    )

    assert tool.executed is False
    assert outcomes[0].result.error == "tool_timeout"
    assert outcomes[0].result.data is not None
    assert outcomes[0].result.data["reason"] == "turn_budget_exhausted"


@pytest.mark.asyncio
async def test_max_duration_is_capped_by_remaining_turn_budget():
    """The tighter of (tool ceiling, turn budget) wins."""
    tool = _BudgetedTool("read", seconds=30.0, hang=True)
    started = time.monotonic()
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("t1", "read")], _ctx(), deadline=Deadline.after(0.05)
    )

    assert outcomes[0].result.error == "tool_timeout"
    assert outcomes[0].result.data is not None
    # The budget comes from a live clock, so compare with a sane tolerance.
    assert outcomes[0].result.data["timeout_seconds"] == pytest.approx(0.05, abs=0.01)
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_parallel_batch_each_call_gets_its_own_budget():
    tools = [
        _BudgetedTool("read", seconds=0.05, hang=True, parallel_safe=True),
        _BudgetedTool("glob", seconds=0.05, hang=True, parallel_safe=True),
    ]
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    dispatcher = ToolDispatcher(registry, PermissionGate())

    started = time.monotonic()
    outcomes = await dispatcher.dispatch_batch(
        [_ref("t1", "read"), _ref("t2", "glob")], _ctx()
    )

    assert [outcome.result.error for outcome in outcomes] == [
        "tool_timeout",
        "tool_timeout",
    ]
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_tool_finished_event_carries_the_error_code():
    tool = _BudgetedTool("read", seconds=0.05, hang=True)
    observer = RecordingObserver()
    registry = ToolRegistry()
    registry.register(tool)
    dispatcher = ToolDispatcher(registry, PermissionGate(), observer=observer)

    await dispatcher.dispatch_batch([_ref("t1", "read")], _ctx())

    finished = observer.of_type(ToolFinished)
    assert len(finished) == 1
    assert finished[0].success is False
    assert finished[0].error == "tool_timeout"
