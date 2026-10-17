"""TDD (M2): ``LoopEvent`` -> wire event mapping (``server/bridge.py``).

The bridge is deliberately a pure mapping + a sink call, so every case here
is offline: no socket, no asyncio, no loop.  ``to_wire`` is the contract the
TS reducer mirrors — a field rename on either side must fail one of these.
"""

from __future__ import annotations

import pytest

from ai_coding.core.events import (
    AssistantMessage,
    AssistantTextDelta,
    BackgroundTaskDone,
    ContextAssembled,
    ContextCompacted,
    Degraded,
    InputSteered,
    IterationStarted,
    LedgerUpdated,
    MemoryConsolidated,
    MemoryHarvested,
    MemoryInjected,
    MemoryRecalled,
    ModelStepFinished,
    ProviderFallback,
    ProviderRetried,
    ScopedEvent,
    TaskUpdated,
    ToolApprovalRequested,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    ToolSurfaceWarning,
    TurnFinished,
)
from ai_coding.domain.ledger import UncertaintyItem
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.task import TaskState
from ai_coding.server.bridge import EventBridge, to_wire

# ---- the 14 mapped kinds ---------------------------------------------------


def test_message_finished_carries_text_and_reasoning() -> None:
    wire = to_wire(AssistantMessage(text="done", reasoning="hmm"))
    assert wire == {"type": "message.finished", "data": {"text": "done", "reasoning": "hmm"}}


def test_message_finished_none_fields_stay_null() -> None:
    wire = to_wire(AssistantMessage(text=None))
    assert wire == {"type": "message.finished", "data": {"text": None, "reasoning": None}}


def test_tool_started_passes_params_through() -> None:
    event = ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls"}, iteration=2)
    wire = to_wire(event)
    assert wire == {
        "type": "tool.started",
        "data": {"callId": "c1", "toolName": "bash", "params": {"command": "ls"}, "iteration": 2},
    }


def test_tool_chunk_passes_stream_and_seq() -> None:
    wire = to_wire(ToolChunk(call_id="c1", tool_name="bash", stream="stdout", text="a\n", seq=7))
    assert wire == {
        "type": "tool.chunk",
        "data": {"callId": "c1", "stream": "stdout", "text": "a\n", "seq": 7},
    }


def test_tool_finished_passes_every_field() -> None:
    wire = to_wire(
        ToolFinished(
            call_id="c1",
            tool_name="bash",
            success=False,
            output_preview="boom",
            duration_ms=42,
            error="tool_error",
            iteration=1,
        )
    )
    assert wire == {
        "type": "tool.finished",
        "data": {
            "callId": "c1",
            "toolName": "bash",
            "success": False,
            "outputPreview": "boom",
            "durationMs": 42,
            "error": "tool_error",
            "iteration": 1,
        },
    }


def test_approval_requested_stays_off_the_event_stream() -> None:
    """M5: approvals use the request round-trip, not a wire event.

    The dispatcher still emits ``ToolApprovalRequested`` for trace observers,
    but mapping it here would duplicate the panel's channel and the old
    auto-approve notice was factually wrong once the panel exists.
    """
    event = ToolApprovalRequested(tool_name="bash", params={"command": "rm x"}, reason="warn")
    assert to_wire(event) is None


def test_plan_updated_carries_the_structured_list() -> None:
    """20 §批次2: the wire carries the list, not a pre-rendered display.

    The old payload (display/done/total) made the UI render small-task-state
    prose; the structured items are what the composer's checkboxes draw from,
    and the model-facing rendering stays backend-side (`TaskState.render`).
    """
    task = TaskState(goal="g")
    task.add("step one")
    task.add("step two")
    task.items[0].status = "completed"
    wire = to_wire(TaskUpdated(task=task))
    assert wire is not None
    assert wire["type"] == "plan.updated"
    assert wire["data"] == {
        "goal": "g",
        "items": [
            {"id": "1", "content": "step one", "status": "completed"},
            {"id": "2", "content": "step two", "status": "pending"},
        ],
    }


def test_ledger_updated_items() -> None:
    item = UncertaintyItem(id="u1", question="is it?", level="high", evidence=("log line",))
    wire = to_wire(LedgerUpdated(items=(item,)))
    assert wire == {
        "type": "ledger.updated",
        "data": {"items": [{"level": "high", "question": "is it?", "evidence": ["log line"]}]},
    }


def test_turn_finished_serialises_status_and_handoff() -> None:
    outcome = TurnOutcome(
        status=TurnStatus.ABORTED,
        reason="user interrupt",
        iterations=3,
        handoff="报告",
    )
    wire = to_wire(TurnFinished(outcome=outcome))
    assert wire == {
        "type": "turn.finished",
        "data": {
            "status": "aborted",
            "reason": "user interrupt",
            "iterations": 3,
            "truncatedByLength": False,
            "handoff": "报告",
            "note": "",
        },
    }


def test_turn_finished_carries_the_plan_note() -> None:
    """stage 4 r2: the plan guard's terminal line rides the outcome."""
    outcome = TurnOutcome(
        status=TurnStatus.COMPLETED,
        reason="plan unsubmitted",
        note="[plan] 本回合未提交计划（未调用 present_plan）；可继续对话让它提交。",
    )
    wire = to_wire(TurnFinished(outcome=outcome))
    assert wire is not None
    assert wire["data"]["note"].startswith("[plan] 本回合未提交计划")


def test_turn_finished_completed_truncated() -> None:
    outcome = TurnOutcome(status=TurnStatus.COMPLETED, truncated_by_length=True)
    wire = to_wire(TurnFinished(outcome=outcome))
    assert wire is not None
    assert wire["data"]["status"] == "completed"
    assert wire["data"]["truncatedByLength"] is True


def test_provider_retried_and_fallback() -> None:
    retried = to_wire(ProviderRetried(kind="timeout", attempt=2, delay_seconds=1.5, reason="slow"))
    assert retried == {
        "type": "provider.retried",
        "data": {"kind": "timeout", "attempt": 2, "delaySeconds": 1.5, "reason": "slow"},
    }
    fallback = to_wire(
        ProviderFallback(from_model="a", to_model="b", kind="error", reason="down")
    )
    assert fallback == {
        "type": "provider.fallback",
        "data": {"fromModel": "a", "toModel": "b", "kind": "error", "reason": "down"},
    }


def test_tools_warning_origins_become_pairs() -> None:
    wire = to_wire(
        ToolSurfaceWarning(
            tools_chars=900,
            mcp_chars=500,
            threshold=800,
            origins=(("mcp", 500), ("builtin", 400)),
        )
    )
    assert wire == {
        "type": "tools.warning",
        "data": {
            "toolsChars": 900,
            "mcpChars": 500,
            "threshold": 800,
            "origins": [["mcp", 500], ["builtin", 400]],
        },
    }


def test_background_task_done() -> None:
    event = BackgroundTaskDone(
        run_id="r1",
        name="scout",
        status="completed",
        summary="found it",
        truncated=True,
        origin_session="s1",
        ledger_path="/tmp/x.json",
    )
    wire = to_wire(event)
    assert wire == {
        "type": "background.done",
        "data": {
            "runId": "r1",
            "name": "scout",
            "status": "completed",
            "summary": "found it",
            "truncated": True,
            "originSession": "s1",
            "ledgerPath": "/tmp/x.json",
        },
    }


def test_context_compacted() -> None:
    wire = to_wire(ContextCompacted(before_messages=12, after_messages=4))
    assert wire == {
        "type": "context.compacted",
        "data": {"beforeMessages": 12, "afterMessages": 4},
    }


def test_subagent_inner_tool_started() -> None:
    inner = ToolStarted(call_id="c9", tool_name="read", params={"path": "/a"}, iteration=1)
    wire = to_wire(ScopedEvent(scope="sub", parent="root", event=inner))
    assert wire == {
        "type": "subagent.event",
        "data": {
            "scope": "sub",
            "inner": {
                "kind": "tool",
                "phase": "started",
                "toolName": "read",
                "params": {"path": "/a"},
            },
        },
    }


def test_subagent_inner_tool_finished() -> None:
    inner = ToolFinished(call_id="c9", tool_name="read", success=True, output_preview="ok")
    wire = to_wire(ScopedEvent(scope="sub", parent="root", event=inner))
    assert wire == {
        "type": "subagent.event",
        "data": {
            "scope": "sub",
            "inner": {
                "kind": "tool",
                "phase": "finished",
                "toolName": "read",
                "success": True,
                "outputPreview": "ok",
                "error": None,
            },
        },
    }


def test_subagent_inner_message_text() -> None:
    inner = AssistantMessage(text="child says")
    wire = to_wire(ScopedEvent(scope="sub", parent="root", event=inner))
    assert wire == {
        "type": "subagent.event",
        "data": {"scope": "sub", "inner": {"kind": "text", "text": "child says"}},
    }


def test_subagent_silent_child_message_is_dropped() -> None:
    inner = AssistantMessage(text=None, reasoning="secret")
    assert to_wire(ScopedEvent(scope="sub", parent="root", event=inner)) is None


def test_subagent_other_inner_events_are_dropped() -> None:
    inner = ToolChunk(call_id="c9", tool_name="read", stream="stdout", text="x")
    assert to_wire(ScopedEvent(scope="sub", parent="root", event=inner)) is None


# ---- the dropped kinds -----------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        IterationStarted(index=1, max_iterations=10),
        AssistantTextDelta(text="chunk"),
        ContextAssembled(iteration=1, system_chars=1),
        ModelStepFinished(index=1),
        Degraded(component="memory", reason="off", fallback="skip"),
        InputSteered(text_chars=3),
        MemoryInjected(note_ids=("n1",), chars=4),
        MemoryRecalled(note_ids=("n1",), scores=(0.5,), chars=4),
        MemoryHarvested(added=("n1",)),
        MemoryConsolidated(merged=("n1",)),
    ],
)
def test_unrendered_events_produce_no_wire(event: object) -> None:
    assert to_wire(event) is None  # type: ignore[arg-type]


# ---- EventBridge (sink + throttle) -----------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _bridge() -> tuple[EventBridge, list[tuple[str, dict]], _Clock]:
    sent: list[tuple[str, dict]] = []
    clock = _Clock()
    bridge = EventBridge(lambda method, params: sent.append((method, params)), clock=clock)
    return bridge, sent, clock


def test_bridge_sink_contract() -> None:
    bridge, sent, _ = _bridge()
    bridge.on_event(ToolStarted(call_id="c1", tool_name="bash", params={}))
    assert sent == [
        (
            "event",
            {
                "type": "tool.started",
                "data": {"callId": "c1", "toolName": "bash", "params": {}, "iteration": 0},
            },
        )
    ]


def test_bridge_drops_unmapped_without_notify() -> None:
    bridge, sent, _ = _bridge()
    bridge.on_event(IterationStarted(index=1, max_iterations=5))
    assert sent == []


def test_bridge_on_delta_is_a_text_delta_notification() -> None:
    bridge, sent, _ = _bridge()
    bridge.on_delta("hello")
    assert sent == [("event", {"type": "text.delta", "data": {"text": "hello"}})]


def test_bridge_throttles_chunks_per_call_id() -> None:
    bridge, sent, clock = _bridge()
    first = ToolChunk(call_id="c1", tool_name="bash", stream="stdout", text="a", seq=1)
    second = ToolChunk(call_id="c1", tool_name="bash", stream="stdout", text="b", seq=2)
    third = ToolChunk(call_id="c1", tool_name="bash", stream="stdout", text="c", seq=3)

    bridge.on_event(first)  # always passes
    clock.now += 0.1
    bridge.on_event(second)  # inside the 150ms window -> dropped
    clock.now += 0.06
    bridge.on_event(third)  # 160ms since the first -> passes

    assert [params["data"]["text"] for _method, params in sent] == ["a", "c"]


def test_bridge_throttle_is_per_call() -> None:
    bridge, sent, clock = _bridge()
    bridge.on_event(ToolChunk(call_id="c1", tool_name="bash", stream="stdout", text="a"))
    clock.now += 0.01
    bridge.on_event(ToolChunk(call_id="c2", tool_name="bash", stream="stdout", text="b"))
    assert [params["data"]["text"] for _method, params in sent] == ["a", "b"]


def test_job_changed_carries_the_panel_rows() -> None:
    """D-1b: the panel's channel. The snapshot travels whole -- the frontend
    replaces its rows rather than replaying deltas, so a dropped event can only
    ever leave it one refresh behind, never half-updated."""
    from ai_coding.core.events import JobChanged

    wire = to_wire(
        JobChanged(
            jobs=(
                {
                    "id": "job_1",
                    "command": "sleep 30",
                    "status": "running",
                    "pid": 42,
                    "log_path": "/tmp/job_1.log",
                    "detail": "",
                    "originSession": "s1",
                    "adopted": False,
                    "startedAt": "2026-10-17T00:00:00Z",
                },
            )
        )
    )

    assert wire == {
        "type": "job.changed",
        "data": {
            "jobs": [
                {
                    "id": "job_1",
                    "command": "sleep 30",
                    "status": "running",
                    "pid": 42,
                    "log_path": "/tmp/job_1.log",
                    "detail": "",
                    "originSession": "s1",
                    "adopted": False,
                    "startedAt": "2026-10-17T00:00:00Z",
                }
            ]
        },
    }


def test_plan_step_receipt_maps_to_the_wire() -> None:
    """E2: the receipt the plan runner appends to the parent transcript now has
    a wire shape; without it a connected TUI only saw the bubbles after a
    reload, while the replay showed them -- live and replay disagreed."""
    from ai_coding.core.events import PlanStepReceipt

    wire = to_wire(
        PlanStepReceipt(prompt="只做这一步，不要做别的：fix bug", summary="[plan] 步骤 1 完成")
    )

    assert wire == {
        "type": "plan.receipt",
        "data": {
            "prompt": "只做这一步，不要做别的：fix bug",
            "summary": "[plan] 步骤 1 完成",
        },
    }
