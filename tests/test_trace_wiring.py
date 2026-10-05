"""循环事件必须进 trace（R4a 设计意图，2026-09 写下但从未接线）。

``AgentLoop.__init__`` 的注释写着 trace recorder "arrives as part of the
observer braid"，``cli._compose_observer`` 也确实写了这个胶水 —— 但它零调用者，
而且即使调用也没用：``_bind_observer`` 每次 ``process_input`` 都会用调用方传的
observer **替换** ``self.observer``。于是 TUI 里 recorder 只收到 MCP 事件，
循环事件（迭代、工具、回合结束）全部丢失，连带丢了 ``summary.json`` 与
``capture: full`` 快照 —— 它们都由 ``TurnFinished`` 触发。
"""

import pytest

from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import (
    RecordingObserver,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.service.session_service import SessionService
from tests.test_agent_loop_react import ScriptedEngine, _registry


class _TraceSink(RecordingObserver):
    """A recorder stand-in: ``AgentLoop`` also calls ``attach_session`` on it."""

    def __init__(self) -> None:
        super().__init__()
        self.session_id = ""
        self.attached: list[object] = []

    def attach_session(self, session, *, start: int = 0) -> None:
        self.attached.append(session)


def _tool_step(name: str = "read", call_id: str = "c1") -> StepResult:
    return StepResult(
        text="", finish_reason="tool_calls",
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments='{"path":"a.py"}')],
    )


def _loop(tmp_path, *, trace=None, observer=None, steps=None):
    from ai_coding.config.models import AIConfig

    engine = ScriptedEngine(steps or [StepResult(text="done", finish_reason="stop")])
    loop = AgentLoop(
        engine, SessionService(root=tmp_path / "s"), _registry(),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=4),
        trace=trace, observer=observer, workspace=str(tmp_path),
    )
    return loop, engine


@pytest.mark.asyncio
async def test_trace_receives_loop_events_through_the_braid(tmp_path):
    """A turn's own events must reach the recorder, not only MCP's."""
    trace = _TraceSink()
    loop, _engine = _loop(tmp_path, trace=trace)
    session = SessionService(root=tmp_path / "s2").create(title="t")

    await loop.process_input(session, "hello", observer=RecordingObserver())

    kinds = [type(e).__name__ for e in trace.events]
    assert "IterationStarted" in kinds
    assert "TurnFinished" in kinds
    assert "ModelStepFinished" in kinds


@pytest.mark.asyncio
async def test_trace_receives_tool_events_for_the_same_call(tmp_path):
    trace = _TraceSink()
    loop, _engine = _loop(tmp_path, trace=trace,
                          steps=[_tool_step(), StepResult(text="done", finish_reason="stop")])
    session = SessionService(root=tmp_path / "s3").create(title="t")

    await loop.process_input(session, "read it", observer=RecordingObserver())

    started = [e for e in trace.events if isinstance(e, ToolStarted)]
    finished = [e for e in trace.events if isinstance(e, ToolFinished)]
    assert [e.tool_name for e in started] == ["read"]
    assert [e.success for e in finished] == [True]


@pytest.mark.asyncio
async def test_the_ui_observer_still_gets_everything(tmp_path):
    """Braid must not cost the UI its own stream."""
    ui = RecordingObserver()
    trace = _TraceSink()
    loop, _engine = _loop(tmp_path, trace=trace)
    session = SessionService(root=tmp_path / "s4").create(title="t")

    await loop.process_input(session, "hello", observer=ui)

    assert [type(e).__name__ for e in ui.events] == [type(e).__name__ for e in trace.events]


@pytest.mark.asyncio
async def test_a_raising_ui_observer_does_not_starve_the_trace(tmp_path):
    """Observation must never break the turn, and one sink must not silence another."""

    class _Angry:
        def on_event(self, event):
            raise RuntimeError("ui is down")

    trace = _TraceSink()
    loop, _engine = _loop(tmp_path, trace=trace)
    session = SessionService(root=tmp_path / "s5").create(title="t")

    outcome = await loop.process_input(session, "hello", observer=_Angry())

    assert outcome is not None
    assert any(isinstance(e, TurnFinished) for e in trace.events)


def test_without_a_trace_the_observer_is_not_wrapped(tmp_path):
    """No trace means no extra layer: the loop's own observer stays as given."""
    loop, _engine = _loop(tmp_path)
    assert not hasattr(loop.observer, "_observers")  # not a Braid



@pytest.mark.asyncio
async def test_trace_is_wired_even_when_the_caller_passes_no_observer(tmp_path):
    """Some callers pass no observer at all — the trace still owes its events.

    Found by running the real loop end to end rather than by reading the code:
    ``process_input(session, text)`` with no ``observer=`` skipped the rebind
    entirely, so the braid never formed and the recorder stayed empty — the same
    bug as before, just through the other door.  The Textual app always passes
    its observer, so this path is the REPL's and any future caller's.
    """
    trace = _TraceSink()
    loop, _engine = _loop(tmp_path, trace=trace)
    session = SessionService(root=tmp_path / "s7").create(title="t")

    await loop.process_input(session, "hello")

    assert [type(e).__name__ for e in trace.events], "no events without a per-call observer"
