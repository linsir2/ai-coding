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
from ai_coding.domain.message import CANCELLED_BY_USER, ToolCallRef, ToolResult
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
        occupies_slot: bool = True,
    ) -> None:
        self._name = name
        self.parallel_safe = parallel_safe
        self.occupies_slot = occupies_slot
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


class _SlowApproval(_Approval):
    """A human who takes a measurable moment to answer."""

    def __init__(self, answer: ApprovalResult | None = None, delay: float = 0.03) -> None:
        super().__init__(answer)
        self.delay = delay

    async def approve(self, tool_name, params, reason):
        self.calls.append((tool_name, params, reason))
        await asyncio.sleep(self.delay)
        return self.answer


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


def _dispatcher(
    tools, *, approval=None, max_parallel=4, observer=None, write_coordinator=None,
    slots=None,
):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolDispatcher(
        registry,
        PermissionGate(),
        approval,
        max_parallel=max_parallel,
        observer=observer,
        write_coordinator=write_coordinator,
        slots=slots,
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
async def test_a10_the_approval_wait_is_reported_apart_from_the_run():
    """The card's number must not be mostly the human thinking."""
    tool = _FakeTool("write")
    observer = RecordingObserver()
    approval = _SlowApproval(ApprovalResult.YES)
    await _dispatcher([tool], approval=approval, observer=observer).dispatch_batch(
        [_ref("c1", "write", '{"path":"a"}')], _ctx()
    )
    finished = observer.of_type(ToolFinished)[0]
    assert finished.approval_ms >= 25
    assert finished.duration_ms >= finished.approval_ms


@pytest.mark.asyncio
async def test_a10_an_allowed_call_reports_no_approval_wait():
    tool = _FakeTool("read")
    observer = RecordingObserver()
    await _dispatcher([tool], observer=observer).dispatch_batch([_ref("c1", "read")], _ctx())
    assert observer.of_type(ToolFinished)[0].approval_ms == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [ApprovalResult.NO, ApprovalResult.STOP])
async def test_a10_a_refused_call_still_reports_how_long_it_waited(answer):
    tool = _FakeTool("write")
    observer = RecordingObserver()
    approval = _SlowApproval(answer)
    await _dispatcher([tool], approval=approval, observer=observer).dispatch_batch(
        [_ref("c1", "write")], _ctx()
    )
    finished = observer.of_type(ToolFinished)[0]
    assert finished.approval_ms >= 25
    assert finished.duration_ms >= finished.approval_ms


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
async def test_an_invented_plan_tool_says_the_block_is_text():
    """The model turned ``<proposed_plan>`` into a tool call; the refusal must
    name the real shape instead of repeating "unknown tool"."""
    for name in ("propose_plan", "proposed_plan", "present_plan"):
        outcomes = await _dispatcher([_FakeTool("read")]).dispatch_batch(
            [_ref("c1", name)], _ctx()
        )
        result = outcomes[0].result
        assert result.success is False
        assert result.error == "unknown_tool"
        assert "<proposed_plan>" in result.output
        assert "文本块" in result.output
        assert "不存在" in result.output


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
async def test_malformed_arguments_are_refused_not_run():
    """R21 §10: ``{}`` used to be executed as if the model had asked for it."""
    tool = _FakeTool("read")
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "read", "{not json")], _ctx()
    )
    assert tool.params_seen == []
    assert outcomes[0].result.error == "invalid_arguments"
    assert outcomes[0].denied is True


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
    # R8c adds a second stamped field, ``kill_event`` - the flag the cancel path
    # sets so a tool that owns a subprocess can kill it; it is per call for the
    # same reason ``call_id`` is.
    received = tool.contexts_seen[0]
    assert received.call_id == "c1"
    assert received.kill_event is not None
    assert dataclasses.replace(received, call_id="", kill_event=None) == ctx


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
        def check(self, tool_name, params, **kwargs):
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
async def test_safe_calls_batch_on_both_sides_of_a_barrier():
    """Consecutive ALLOW+safe calls form one concurrent group each side of a
    barrier (dsh's runGroup); the barrier itself overlaps neither side."""
    from ai_coding.core.dispatch import ToolDispatcher
    from ai_coding.security.permission_gate import Decision, PermissionDecision

    probe = _ConcurrencyProbe()
    tools = [
        _FakeTool("read0", parallel_safe=True, on_execute=probe.enter),
        _FakeTool("read1", parallel_safe=True, on_execute=probe.enter),
        _FakeTool("barrier", parallel_safe=False, on_execute=probe.enter),
        _FakeTool("read2", parallel_safe=True, on_execute=probe.enter),
        _FakeTool("read3", parallel_safe=True, on_execute=probe.enter),
    ]
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    class _AllowAll(PermissionGate):
        def check(self, tool_name, params, **kwargs):
            return Decision(PermissionDecision.ALLOW, "")

    outcomes = await ToolDispatcher(registry, _AllowAll()).dispatch_batch(
        [
            _ref("c1", "read0"),
            _ref("c2", "read1"),
            _ref("c3", "barrier"),
            _ref("c4", "read2"),
            _ref("c5", "read3"),
        ],
        _ctx(),
    )
    assert probe.peak == 2
    assert [o.call.id for o in outcomes] == ["c1", "c2", "c3", "c4", "c5"]


@pytest.mark.asyncio
async def test_a_halt_stops_the_batch_at_its_group_boundary():
    """A STOP on a WARN call halts the batch; the safe run before it already
    finished together, and the call after it is skipped, not run."""
    probe = _ConcurrencyProbe()
    a = _FakeTool("read", parallel_safe=True, on_execute=probe.enter)
    b = _FakeTool("glob", parallel_safe=True, on_execute=probe.enter)
    write = _FakeTool("write")
    tail = _FakeTool("grep", parallel_safe=True, on_execute=probe.enter)
    outcomes = await _dispatcher(
        [a, b, write, tail], approval=_Approval(ApprovalResult.STOP)
    ).dispatch_batch(
        [
            _ref("c1", "read"),
            _ref("c2", "glob"),
            _ref("c3", "write"),
            _ref("c4", "grep"),
        ],
        _ctx(),
    )
    assert probe.peak == 2
    assert [o.call.id for o in outcomes] == ["c1", "c2", "c3", "c4"]
    assert outcomes[2].halted is True
    assert outcomes[3].result.error == "skipped"
    assert not tail.executed


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
        def check(self, tool_name, params, **kwargs):
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


# ---- R8c: cancelling one batch, not the turn (docs/r8-analysis.md 3.3-S2) ---


class _HangingTool(BaseTool):
    """Never returns on its own; only a cancel (or a deadline) ends it."""

    parallel_safe = True

    def __init__(self, name: str = "read") -> None:
        self._name = name
        self.kill_event: object | None = None
        self.finished = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "hangs"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        self.kill_event = None if ctx is None else ctx.kill_event
        await asyncio.sleep(30)
        self.finished = True  # pragma: no cover - the cancel wins first
        return ToolResult(success=True, output="never")


@pytest.mark.asyncio
async def test_r8_a_cancelled_call_comes_back_structured():
    cancel = asyncio.Event()
    tool = _HangingTool()
    registry = ToolRegistry()
    registry.register(tool)
    dispatcher = ToolDispatcher(registry, PermissionGate(), cancel=cancel)

    async def trip() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    batches = await asyncio.gather(
        dispatcher.dispatch_batch([_ref("t1", "read")], _ctx()), trip()
    )
    outcome = batches[0][0]

    assert outcome.result.success is False
    assert outcome.result.error == CANCELLED_BY_USER
    assert outcome.denied is False
    assert outcome.halted is False  # the turn carries on


@pytest.mark.asyncio
async def test_r8_the_cancel_reaches_the_tool_so_a_child_process_can_die():
    """The tool gets a thread-safe flag; bash hands it to the process runner."""
    cancel = asyncio.Event()
    tool = _HangingTool()
    registry = ToolRegistry()
    registry.register(tool)
    dispatcher = ToolDispatcher(registry, PermissionGate(), cancel=cancel)

    async def trip() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    await asyncio.gather(dispatcher.dispatch_batch([_ref("t1", "read")], _ctx()), trip())
    await asyncio.sleep(0.05)

    assert tool.kill_event is not None
    assert tool.kill_event.is_set() is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_r8_a_cancelled_call_does_not_disturb_its_siblings():
    cancel = asyncio.Event()
    slow = _HangingTool("read")
    fast = _FakeTool("glob", parallel_safe=True, result=ToolResult(success=True, output="ok"))
    registry = ToolRegistry()
    registry.register(slow)
    registry.register(fast)
    dispatcher = ToolDispatcher(registry, PermissionGate(), cancel=cancel)

    async def trip() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    batches = await asyncio.gather(
        dispatcher.dispatch_batch([_ref("t1", "read"), _ref("t2", "glob")], _ctx()), trip()
    )
    outcomes = batches[0]

    assert outcomes[0].result.error == CANCELLED_BY_USER
    # Both are in the same parallel batch, so the sibling is abandoned too - but
    # it still gets a *result*: the declared tool_calls must pair up.
    assert all(outcome.result is not None for outcome in outcomes)


# ---- T1: precheck refusal happens before approval ---------------------------


class _PrecheckedWrite(_FakeTool):
    """A write-shaped fake whose precheck refuses on its own account.

    B gave ``sandbox_violation`` a new meaning -- an operator question, not a
    wall -- so it is no longer what this fake reports.  What is left of the rule
    is the part that still holds: a refusal the *tool* owns (an empty write, a
    stale read) is not the operator's to decide, so it must not grow a prompt.
    The sandbox's own refusal is covered by
    ``tests/test_outside_read_approval_flow.py``.
    """

    def precheck(self, params):
        return ToolResult(success=False, output="nothing to write", error="tool_error")


@pytest.mark.asyncio
async def test_precheck_refusal_skips_approval_and_execution():
    tool = _PrecheckedWrite("write")
    approval = _NeverAsked()
    dispatcher = _dispatcher([tool], approval=approval)

    outcomes = await dispatcher.dispatch_batch(
        [_ref("c1", "write", '{"path": "/outside/x.py"}')], _ctx()
    )

    assert outcomes[0].result.error == "tool_error"
    assert outcomes[0].denied is True
    assert tool.executed is False


# ---- P2-B1: write coordination before execution ---------------------------
#
# The claim sits after the write-ahead replay and before ``_execute``: the
# params are final (guard/hook rewrites have already landed) and nothing has
# been touched yet.  A claim that cannot be taken is a structured
# ``write_conflict``, and the replay branch keeps its meaning (a replayed call
# did run before the crash, so it takes no claim now).


class _Claim:
    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator
        self.released = 0

    def release(self) -> None:
        self.released += 1
        self._coordinator.releases.append(self)


class _Coordinator:
    """Records what the dispatcher asked for; grants or refuses on demand."""

    def __init__(self, *, grant: bool = True) -> None:
        self.grant = grant
        self.claims: list[dict] = []
        self.releases: list[_Claim] = []

    async def claim(self, owner, root, paths, timeout):
        self.claims.append(
            {"owner": owner, "root": root, "paths": tuple(paths), "timeout": timeout}
        )
        return _Claim(self) if self.grant else None

    def acquire_hold(self, owner, root):
        return f"hold-{owner}"

    def release_hold(self, token):
        return None

    def register_run(self, key, paths, origin_session):
        return None

    def overlap(self, key, paths):
        return ()


class _WriteAheadStub:
    def __init__(self, replay=None) -> None:
        self._replay = replay
        self.abandons: list = []

    def claim(self, call, *, allow_replay=False):
        return self._replay

    def settle(self, call, result):
        return None

    def abandon(self, call):
        self.abandons.append(call)


class _PathTool(_FakeTool):
    """A fake whose declared write scope the dispatcher must claim."""

    def __init__(self, name: str, paths, **kwargs) -> None:
        super().__init__(name, **kwargs)
        self._paths = paths

    def write_paths_for(self, params, ctx):
        return self._paths


@pytest.mark.asyncio
async def test_a_write_claim_uses_the_declared_paths_and_owner():
    tool = _PathTool("read", ("/tmp/ws/a.py",))
    coordinator = _Coordinator()
    await _dispatcher([tool], write_coordinator=coordinator).dispatch_batch(
        [_ref("c1", "read")], _ctx()
    )

    assert len(coordinator.claims) == 1
    claim = coordinator.claims[0]
    assert claim["owner"] == "sid"
    assert claim["root"] == "/tmp/ws"
    assert claim["paths"] == ("/tmp/ws/a.py",)
    # No turn deadline here, so the bounded wait is the 30s default.
    assert claim["timeout"] == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_a_write_claim_is_released_after_the_call():
    tool = _PathTool("read", ("/tmp/ws/a.py",))
    coordinator = _Coordinator()
    await _dispatcher([tool], write_coordinator=coordinator).dispatch_batch(
        [_ref("c1", "read")], _ctx()
    )

    assert tool.executed is True
    assert len(coordinator.releases) == 1
    assert coordinator.releases[0].released == 1


@pytest.mark.asyncio
async def test_an_unknown_write_scope_claims_the_whole_workspace():
    tool = _PathTool("bash", ())
    coordinator = _Coordinator()
    await _dispatcher(
        [tool], approval=_Approval(ApprovalResult.YES), write_coordinator=coordinator
    ).dispatch_batch([_ref("c1", "bash", '{"command":"echo hi"}')], _ctx())

    assert coordinator.claims[0]["paths"] == ()


def test_shell_read_declares_no_write_scope():
    """P2-B1 fix: ``ShellReadTool`` inherits ``BashTool``'s opaque ``()``; a
    plan-mode inspection must not take the whole-workspace claim."""
    from ai_coding.tools.shell_read_tool import ShellReadTool

    assert ShellReadTool().write_paths_for({"command": "git status"}, None) is None


@pytest.mark.asyncio
async def test_a_read_only_tool_never_claims_even_with_an_opaque_scope():
    tool = _PathTool("shell_read", ())
    tool.mutates_world = False  # what ShellReadTool declares
    coordinator = _Coordinator()
    outcomes = await _dispatcher([tool], write_coordinator=coordinator).dispatch_batch(
        [_ref("c1", "shell_read", '{"command":"git status"}')], _ctx()
    )

    assert outcomes[0].result.success is True
    assert coordinator.claims == [], "read-only inspection takes no write claim"


@pytest.mark.asyncio
async def test_write_and_bash_still_claim_the_scope_they_declare():
    for tool, ref in (
        (_PathTool("write", ("/tmp/ws/a.py",)), _ref("c1", "write", '{"path":"a.py"}')),
        (_PathTool("bash", ()), _ref("c2", "bash", '{"command":"echo hi"}')),
    ):
        coordinator = _Coordinator()
        await _dispatcher(
            [tool], approval=_Approval(ApprovalResult.YES), write_coordinator=coordinator
        ).dispatch_batch([ref], _ctx())
        assert len(coordinator.claims) == 1, tool.name


@pytest.mark.asyncio
async def test_a_cancelled_claim_wait_leaves_the_scope_claimable(tmp_path):
    """P2-B: cancelling the task waiting for a claim must not leak the wait."""
    from ai_coding.core.write_coordinator import WriteCoordinator

    coordinator = WriteCoordinator(tmp_path)
    workspace = str(tmp_path)
    target = str(tmp_path / "a.py")
    held = await coordinator.claim("other", workspace, (target,), timeout=1.0)
    assert held is not None

    tool = _PathTool("write", (target,))
    dispatcher = _dispatcher(
        [tool],
        approval=_Approval(ApprovalResult.YES),
        write_coordinator=coordinator,
    )
    waiter = asyncio.create_task(
        dispatcher.dispatch_batch(
            [_ref("c1", "write", '{"path":"a.py"}')],
            dataclasses.replace(_ctx(), workspace=workspace),
        )
    )
    await asyncio.sleep(0.1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert tool.executed is False
    held.release()
    second = await coordinator.claim("sid", workspace, (target,), timeout=0.5)
    assert second is not None, "a cancelled waiter must not hold the scope"
    second.release()


def test_mcp_call_declares_a_workspace_wide_write_scope():
    from types import SimpleNamespace

    from ai_coding.tools.mcp_call import MCPCallTool

    tool = MCPCallTool(SimpleNamespace())
    assert tool.write_paths_for({}, None) == ()


def test_a_write_tool_resolves_its_path_for_the_claim():
    """The declared scope is the *resolved* target, not the model's spelling."""
    from pathlib import Path

    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.write_tool import WriteTool

    workspace = Path("/tmp/ws")
    tool = WriteTool(Sandbox(str(workspace)), FileStateTracker())
    assert tool.write_paths_for({"path": "sub/a.py"}, None) == (
        str(workspace.resolve() / "sub" / "a.py"),
    )
    # A path the sandbox refuses cannot run anyway (precheck refuses it), so it
    # declares no scope and never takes a claim.
    assert tool.write_paths_for({"path": "/etc/passwd"}, None) is None


def test_subagent_tools_declare_no_write_scope():
    from ai_coding.tools.agents_tool import SpawnAgentTool
    from ai_coding.tools.subagent_tool import SubAgentTool

    assert SubAgentTool(None).write_paths_for({"task": "x"}, None) is None
    assert SpawnAgentTool(None).write_paths_for({"task": "x"}, None) is None


@pytest.mark.asyncio
async def test_a_subagent_call_never_claims_the_workspace():
    from ai_coding.domain.subagent import SubagentResult
    from ai_coding.tools.subagent_tool import SubAgentTool

    class _Runner:
        async def run(self, task, name="sub-agent", **kwargs):
            return SubagentResult(status="completed", conclusion="done")

    coordinator = _Coordinator()
    outcomes = await _dispatcher(
        [SubAgentTool(_Runner())],
        approval=_Approval(ApprovalResult.YES),
        write_coordinator=coordinator,
    ).dispatch_batch([_ref("c1", "subAgent", '{"task":"scan"}')], _ctx())

    assert outcomes[0].result.success is True
    assert coordinator.claims == [], "a child manages its own writes"


@pytest.mark.asyncio
async def test_a_write_conflict_is_structured_and_abandons_the_intent():
    tool = _PathTool("read", ("/tmp/ws/a.py",))
    coordinator = _Coordinator(grant=False)
    log = _WriteAheadStub()
    ctx = dataclasses.replace(_ctx(), write_ahead=log)
    outcomes = await _dispatcher([tool], write_coordinator=coordinator).dispatch_batch(
        [_ref("c1", "read")], ctx
    )

    result = outcomes[0].result
    assert tool.executed is False
    assert outcomes[0].denied is True
    assert result.success is False
    assert result.error == "write_conflict"
    assert result.data is not None
    assert result.data["paths"] == ["/tmp/ws/a.py"]
    assert result.data["owner"] == "sid"
    assert isinstance(result.data["wait_ms"], int)
    # The call never ran, so the running intent it wrote must not survive.
    assert [call.id for call in log.abandons] == ["c1"]


@pytest.mark.asyncio
async def test_a_claim_is_released_when_the_tool_raises():
    tool = _PathTool("read", ("/tmp/ws/a.py",), raiser=RuntimeError("boom"))
    coordinator = _Coordinator()
    outcomes = await _dispatcher([tool], write_coordinator=coordinator).dispatch_batch(
        [_ref("c1", "read")], _ctx()
    )

    assert outcomes[0].result.error == "tool_error"
    assert len(coordinator.releases) == 1


@pytest.mark.asyncio
async def test_a_replayed_call_takes_no_claim():
    tool = _PathTool("write", ("/tmp/ws/a.py",))
    coordinator = _Coordinator()
    log = _WriteAheadStub(replay=ToolResult(success=True, output="cached result"))
    outcomes = await _dispatcher(
        [tool], approval=_Approval(ApprovalResult.YES), write_coordinator=coordinator
    ).dispatch_batch(
        [_ref("c1", "write", '{"path":"a.py"}')],
        dataclasses.replace(_ctx(), write_ahead=log),
        allow_replay=True,
    )

    assert outcomes[0].result.output == "cached result"
    assert coordinator.claims == [], "the call ran before the crash; it needs no claim"
    assert tool.executed is False


@pytest.mark.asyncio
async def test_a_refusal_speaks_chinese_but_keeps_its_code():
    """A7 T5: two halves of the same result, asserted together — changing one
    without the other is a half-done fix.  `error` is the machine code the TUI
    maps through toolFailure(); `output` is what the model reads and what the
    ledger's evidence line shows the human."""
    for approval, expected_code, expected_text in (
        (ApprovalResult.NO, "not_approved", "未批准"),
        (ApprovalResult.STOP, "stopped", "停止"),
    ):
        tool = _FakeTool("write")
        outcomes = await _dispatcher([tool], approval=_Approval(approval)).dispatch_batch(
            [_ref("c1", "write")], _ctx()
        )
        result = outcomes[0].result
        assert result.success is False
        assert result.error == expected_code
        assert expected_text in result.output
        assert result.output.isascii() is False  # no English boilerplate left


# ---- P2-C1: tool groups consume the one execution pool ---------------------


class _SlotProbe:
    """Records the pool's active count at each execution."""

    def __init__(self, slots) -> None:
        self.slots = slots
        self.levels: list[int] = []

    async def see(self) -> None:
        self.levels.append(self.slots.active)


@pytest.mark.asyncio
async def test_a_normal_group_takes_one_slot_and_returns_it():
    from ai_coding.core.execution_slots import ExecutionSlotPool

    pool = ExecutionSlotPool(2)
    probe = _SlotProbe(pool)
    a = _FakeTool("read", parallel_safe=True, on_execute=lambda _p: probe.see())
    b = _FakeTool("glob", parallel_safe=True, on_execute=lambda _p: probe.see())

    outcomes = await _dispatcher([a, b], slots=pool).dispatch_batch(
        [_ref("c1", "read"), _ref("c2", "glob")], _ctx()
    )

    assert [outcome.result.success for outcome in outcomes] == [True, True]
    assert probe.levels == [1, 1], "one group holds exactly one slot"
    assert pool.active == 0, "the group releases it when it drains"


@pytest.mark.asyncio
async def test_an_orchestration_group_does_not_take_a_slot():
    from ai_coding.core.execution_slots import ExecutionSlotPool

    pool = ExecutionSlotPool(2)
    probe = _SlotProbe(pool)
    a = _FakeTool(
        "read", parallel_safe=True, occupies_slot=False,
        on_execute=lambda _p: probe.see(),
    )
    b = _FakeTool(
        "glob", parallel_safe=True, occupies_slot=False,
        on_execute=lambda _p: probe.see(),
    )

    await _dispatcher([a, b], slots=pool).dispatch_batch(
        [_ref("c1", "read"), _ref("c2", "glob")], _ctx()
    )

    assert probe.levels == [0, 0]
    assert pool.active == 0


@pytest.mark.asyncio
async def test_groups_split_when_occupancy_differs_and_keep_result_order():
    from ai_coding.core.execution_slots import ExecutionSlotPool

    pool = ExecutionSlotPool(2)
    probe = _SlotProbe(pool)
    read = _FakeTool("read", parallel_safe=True, on_execute=lambda _p: probe.see())
    orchestration = _FakeTool(
        "list_agents", parallel_safe=True, occupies_slot=False,
        on_execute=lambda _p: probe.see(),
    )
    glob = _FakeTool("glob", parallel_safe=True, on_execute=lambda _p: probe.see())

    outcomes = await _dispatcher([read, orchestration, glob], slots=pool).dispatch_batch(
        [_ref("c1", "read"), _ref("c2", "list_agents"), _ref("c3", "glob")], _ctx()
    )

    assert [outcome.call.id for outcome in outcomes] == ["c1", "c2", "c3"]
    assert probe.levels == [1, 0, 1]


@pytest.mark.asyncio
async def test_a_child_that_already_holds_a_slot_does_not_ask_again():
    from ai_coding.core.execution_slots import ExecutionSlotPool, enter_child

    pool = ExecutionSlotPool(1)
    probe = _SlotProbe(pool)
    parent_slot = pool.try_reserve("child")
    assert parent_slot is not None
    a = _FakeTool("read", parallel_safe=True, on_execute=lambda _p: probe.see())
    b = _FakeTool("glob", parallel_safe=True, on_execute=lambda _p: probe.see())

    with enter_child():
        await _dispatcher([a, b], slots=pool).dispatch_batch(
            [_ref("c1", "read"), _ref("c2", "glob")], _ctx()
        )

    assert probe.levels == [1, 1]
    assert pool.active == 1
    parent_slot.release()


@pytest.mark.asyncio
async def test_single_orchestration_call_takes_no_slot():
    from ai_coding.core.execution_slots import ExecutionSlotPool

    pool = ExecutionSlotPool(1)
    probe = _SlotProbe(pool)
    tool = _FakeTool(
        "subAgent", occupies_slot=False, on_execute=lambda _p: probe.see()
    )

    await _dispatcher([tool], slots=pool).dispatch_batch(
        [_ref("c1", "subAgent")], _ctx()
    )

    assert probe.levels == [0]
    assert pool.active == 0


@pytest.mark.asyncio
async def test_single_normal_call_takes_a_slot():
    from ai_coding.core.execution_slots import ExecutionSlotPool

    pool = ExecutionSlotPool(1)
    probe = _SlotProbe(pool)
    tool = _FakeTool("read", on_execute=lambda _p: probe.see())

    await _dispatcher([tool], slots=pool).dispatch_batch([_ref("c1", "read")], _ctx())

    assert probe.levels == [1]
    assert pool.active == 0


@pytest.mark.asyncio
async def test_without_a_pool_the_old_semaphore_still_bounds_a_group():
    """Callers that never received a pool keep the pre-P2-C1 behaviour."""
    probe = _ConcurrencyProbe()
    names = ["read", "glob", "grep"]
    tools = [
        _FakeTool(name, parallel_safe=True, on_execute=probe.enter)
        for name in names
    ]
    outcomes = await _dispatcher(tools, max_parallel=1).dispatch_batch(
        [_ref(f"c{i}", name) for i, name in enumerate(names)], _ctx()
    )

    assert probe.peak == 1
    assert all(outcome.result.success for outcome in outcomes)

