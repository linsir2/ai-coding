"""TDD (R1a-3): loop event contract + observers.

Events are the loop's only outward channel.  They are frozen values (they record
things that already happened) and they are the reason a 10MB tool output can
never leak into the UI or a log line.
"""

import asyncio
import dataclasses

import pytest

from ai_coding.core.events import (
    OUTPUT_PREVIEW_CHARS,
    AssistantMessage,
    AssistantTextDelta,
    IterationStarted,
    LoopEvent,
    NullObserver,
    ProviderFallback,
    ProviderRetried,
    RecordingObserver,
    TaskUpdated,
    ToolApprovalRequested,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    preview,
)
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.task import TaskState


def _all_events() -> list[object]:
    ref = ToolCallRef(id="c1", name="read", arguments="{}")
    return [
        IterationStarted(index=1, max_iterations=10),
        AssistantTextDelta(text="hi"),
        AssistantMessage(text="hi", tool_calls=(ref,)),
        ToolApprovalRequested(tool_name="write", params={"path": "a"}, reason="needs confirmation"),
        ToolStarted(call_id="c1", tool_name="read", params={}),
        ToolFinished(
            call_id="c1", tool_name="read", success=True, output_preview="ok", duration_ms=3
        ),
        TaskUpdated(task=TaskState(goal="g")),
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED)),
    ]


def test_events_are_frozen():
    event = ToolStarted(call_id="c1", tool_name="read", params={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.tool_name = "write"  # type: ignore[misc]
    finished = ToolFinished(
        call_id="c1", tool_name="read", success=True, output_preview="", duration_ms=0
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        finished.success = False  # type: ignore[misc]


def test_event_fields_carry_through():
    event = ToolFinished(
        call_id="c1",
        tool_name="read",
        success=False,
        output_preview="boom",
        duration_ms=12,
    )
    assert event.call_id == "c1"
    assert event.tool_name == "read"
    assert event.success is False
    assert event.output_preview == "boom"
    assert event.duration_ms == 12


def test_null_observer_never_raises():
    observer = NullObserver()
    for event in _all_events():
        assert observer.on_event(event) is None  # type: ignore[arg-type]


def test_recording_observer_collects_and_filters():
    observer = RecordingObserver()
    for event in _all_events():
        observer.on_event(event)  # type: ignore[arg-type]

    assert len(observer.events) == len(_all_events())
    finished = observer.of_type(ToolFinished)
    assert len(finished) == 1
    assert finished[0].call_id == "c1"
    assert observer.of_type(AssistantTextDelta)[0].text == "hi"
    # filtering returns only the requested type, never the whole stream
    started = observer.of_type(ToolStarted)
    assert len(started) == 1
    assert started[0].tool_name == "read"


def test_preview_truncates_long_text():
    assert preview("short") == "short"
    assert preview("") == ""

    long_text = "x" * (OUTPUT_PREVIEW_CHARS * 3)
    out = preview(long_text)
    assert len(out) <= OUTPUT_PREVIEW_CHARS + 1
    assert out.endswith("…")


def test_preview_honours_custom_limit():
    assert preview("abcdef", limit=3) == "abc…"
    assert preview("abc", limit=3) == "abc"


# ---- R4a: the observability events ----------------------------------------


def test_r4_events_are_frozen_and_in_the_union():
    from typing import get_args

    from ai_coding.core.events import (
        ContextAssembled,
        Degraded,
        LoopEvent,
        ModelStepFinished,
        ScopedEvent,
    )

    union = set(get_args(LoopEvent))
    for event_type in (
        ModelStepFinished,
        ContextAssembled,
        Degraded,
        ScopedEvent,
    ):
        assert event_type in union
    # frozen values: a trace recorder must never be able to mutate history
    finished = ModelStepFinished(index=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        finished.index = 2  # type: ignore[misc]
    degraded = Degraded(component="trace_write", reason="disk full")
    with pytest.raises(dataclasses.FrozenInstanceError):
        degraded.component = "other"  # type: ignore[misc]


def test_r4_model_step_finished_carries_usage_and_cache():
    from ai_coding.core.events import ModelStepFinished

    event = ModelStepFinished(
        index=2,
        finish_reason="stop",
        text_chars=12,
        tool_call_count=1,
        usage={"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107},
        cached_tokens=80,
        latency_ms=1234,
    )
    assert event.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 7,
        "total_tokens": 107,
    }
    assert event.cached_tokens == 80
    assert event.cache_write_tokens is None
    assert event.latency_ms == 1234


def test_r4_context_assembled_defaults_to_zero_counts():
    from ai_coding.core.events import ContextAssembled

    event = ContextAssembled(iteration=1, system_chars=10)
    assert event.tools_chars == 0
    assert event.tail_blocks == ()
    assert event.prefix_shared_ratio == 0.0
    assert event.digest == ""


def test_r4_tool_events_default_to_iteration_zero():
    started = ToolStarted(call_id="c1", tool_name="read", params={})
    assert started.iteration == 0
    finished = ToolFinished(
        call_id="c1", tool_name="read", success=True, output_preview="", duration_ms=1
    )
    assert finished.iteration == 0


def test_r4_scoped_event_wraps_any_loop_event():
    from ai_coding.core.events import ScopedEvent

    inner = ToolStarted(call_id="c1", tool_name="read", params={})
    scoped = ScopedEvent(scope="subagent:c9", parent="c9", event=inner)
    assert scoped.event is inner
    assert scoped.parent == "c9"
    # it is itself a LoopEvent, so a scoped stream fans out through the same seam
    assert isinstance(scoped, ScopedEvent)


# ---- R8c: streamed tool output is an event, never content ------------------


def test_r8_tool_chunk_is_a_frozen_loop_event():
    from typing import get_args

    from ai_coding.core.events import LoopEvent, ToolChunk

    assert ToolChunk in set(get_args(LoopEvent))
    chunk = ToolChunk(call_id="c1", tool_name="bash", stream="stdout", text="hi\n", seq=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunk.text = "other"  # type: ignore[misc]
    assert (chunk.call_id, chunk.stream, chunk.seq) == ("c1", "stdout", 3)


# ---- provider retry visibility (M1) ---------------------------------------


def test_provider_retried_is_in_the_event_union():
    """A retry is a fact the loop must be able to state; an event that is not in
    the union cannot travel, and the trace would never see it."""
    from typing import get_args

    assert ProviderRetried in set(get_args(LoopEvent))


def test_provider_retried_is_frozen():
    assert dataclasses.is_dataclass(ProviderRetried)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ProviderRetried(kind="server_error", attempt=1, delay_seconds=2.0).kind = "other"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_the_loop_surfaces_an_engine_retry_to_its_observer(tmp_path):
    """The end-to-end claim, not the unit one: a flaky provider must put a
    ProviderRetried on the loop's own observer.  The engine test proves the
    callback fires; only this proves the loop is listening to it."""
    from ai_coding.ai.openai_engine import OpenAIChatEngine
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.model_config import ModelConfig
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry
    from tests.test_openai_engine import _rate_limited, _SequencedClient, _text_chunk

    observer = RecordingObserver()
    engine = OpenAIChatEngine(
        ModelConfig(name="m", base_url="http://x", api_key="k", retry_base_seconds=0.0),
        client=_SequencedClient([_rate_limited(), [_text_chunk("好", finish_reason="stop")]]),
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(engine, sessions, ToolRegistry(), observer=observer)

    assert await loop.process_input(sessions.create(), "hi") == "好"

    retried = [e for e in observer.events if isinstance(e, ProviderRetried)]
    assert [(e.kind, e.attempt) for e in retried] == [("rate_limited", 1)]


def test_provider_retried_lands_in_the_trace_with_its_span(tmp_path):
    import json

    """The recorder needs no branch for a new event -- it writes any dataclass
    by class name -- so this test guards the *wiring*, not the recorder: a retry
    that never reaches disk is invisible after the turn ends."""
    from ai_coding.core.trace import TraceRecorder, TraceStore

    store = TraceStore(tmp_path / "traces", max_turn_bytes=1_000_000)
    recorder = TraceRecorder(store, session_id="sess-1", capture="full")
    recorder.on_event(IterationStarted(index=1, max_iterations=10))
    recorder.on_event(
        ProviderRetried(kind="server_error", attempt=2, delay_seconds=4.0, reason="502")
    )

    rows = [
        json.loads(line)
        for path in store.root.glob("**/*.jsonl")
        for line in path.read_text().splitlines()
    ]
    hit = [r for r in rows if r["evt"] == "ProviderRetried"]
    assert len(hit) == 1
    assert hit[0]["span"] == "i1", "a retry belongs to the iteration that hit it"
    assert hit[0]["data"] == {
        "kind": "server_error",
        "attempt": 2,
        "delay_seconds": 4.0,
        "reason": "502",
    }


# ---- M2: the operator is told which model is answering ---------------------


def test_provider_fallback_is_in_the_event_union():
    from typing import get_args

    assert ProviderFallback in set(get_args(LoopEvent))


def test_provider_fallback_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ProviderFallback(from_model="qwen", to_model="deepseek", kind="rate_limited").kind = "x"  # type: ignore[misc]




def test_the_loop_turns_a_switch_into_an_event(tmp_path):
    """The end-to-end wiring, as in M1: the unit tests prove the engine reports,
    only this proves the loop is listening."""
    import asyncio

    from ai_coding.ai.engine import FallbackWorthwhile
    from ai_coding.ai.fallback_engine import FallbackEngine
    from ai_coding.domain.loop import StepResult
    from ai_coding.domain.model_config import ModelConfig

    class _Dead:
        def __init__(self, name):
            self.model_config = ModelConfig(name=name, base_url="https://x/v1", api_key="k")
            self.on_retry = None
            self.on_fallback = None

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            raise FallbackWorthwhile("rate_limited: 429", kind="rate_limited")

        async def aclose(self):
            return None

    class _Alive(_Dead):
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return StepResult(
                text="ok", tool_calls=(), finish_reason="stop", usage=None, reasoning=None
            )

    observer = RecordingObserver()
    engine = FallbackEngine([_Dead("qwen"), _Alive("deepseek")])

    async def run():
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.service.session_service import SessionService
        from ai_coding.tools.registry import ToolRegistry

        sessions = SessionService(tmp_path / "sessions")
        loop = AgentLoop(engine, sessions, ToolRegistry(), observer=observer)
        return await loop.process_input(sessions.create(), "hi")

    assert asyncio.run(run()) == "ok"

    switches = [e for e in observer.events if isinstance(e, ProviderFallback)]
    assert [(e.from_model, e.to_model, e.kind) for e in switches] == [
        ("qwen", "deepseek", "rate_limited")
    ]


def test_model_step_finished_carries_the_model_that_answered():
    """G7: the trace records per-step usage already; without the model name the
    spend cannot be attributed to anyone."""
    from ai_coding.core.events import ModelStepFinished

    event = ModelStepFinished(index=1, model="deepseek-chat")

    assert event.model == "deepseek-chat"


def test_model_step_finished_defaults_to_no_model():
    """Optional, so every existing construction keeps working and "nobody said"
    stays distinguishable from a fabricated name."""
    from ai_coding.core.events import ModelStepFinished

    assert ModelStepFinished(index=1).model == ""


# ---- A6: a step that thought and said nothing -------------------------------
#
# The provider can end a step with no text and no tool call.  ChatMessage
# refuses such a step on purpose, so the loop has to decide the turn's fate
# instead of letting the domain error escape to the IPC catch-all.

def _step_engine(step):
    """A one-model engine that always answers `step`; the same shape as the fake
    in test_the_loop_turns_a_switch_into_an_event, without the chain."""
    from ai_coding.ai.engine import ChatEngine
    from ai_coding.domain.model_config import ModelConfig

    class _One(ChatEngine):
        def __init__(self):
            self.model_config = ModelConfig(name="m", base_url="https://x/v1", api_key="k")
            self.on_retry = None
            self.on_fallback = None

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return step

        async def aclose(self):
            return None

    return _One()


def _turn_with(tmp_path, step):
    """Run one real turn whose only model answer is `step`.

    Returns ``(session, observer)``: the session proves what was persisted, the
    observer proves what the UI was told — and TurnFinished is the contract the
    frontend actually renders.
    """
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry

    async def run():
        sessions = SessionService(tmp_path / "sessions")
        observer = RecordingObserver()
        loop = AgentLoop(_step_engine(step), sessions, ToolRegistry(), observer=observer)
        session = sessions.create()
        await loop.process_input(session, "hi")
        return session, observer

    return asyncio.run(run())


def _finished(observer):
    turns = [e for e in observer.events if isinstance(e, TurnFinished)]
    assert len(turns) == 1, f"expected exactly one TurnFinished, got {turns}"
    return turns[0]


def test_an_empty_step_ends_the_turn_in_words_not_in_a_python_message(tmp_path):
    """A6 T1: the screen shows the reason, so the reason must be readable."""
    from ai_coding.domain.loop import StepResult, TurnStatus

    _session, observer = _turn_with(
        tmp_path,
        StepResult(
            text=None, tool_calls=(), finish_reason="stop", usage=None, reasoning="想了一下"
        ),
    )
    outcome = _finished(observer).outcome
    assert outcome.status is TurnStatus.ERROR
    assert "没有回答" in outcome.reason
    assert "stop" in outcome.reason
    assert "ValueError" not in outcome.reason
    assert "EmptyStepError" not in outcome.reason


def test_an_empty_step_persists_no_empty_assistant_message(tmp_path):
    """A6 T2: the invariant still holds — nothing degenerate reaches disk."""
    from ai_coding.domain.loop import StepResult

    session, _observer = _turn_with(
        tmp_path,
        StepResult(
            text=None, tool_calls=(), finish_reason="stop", usage=None, reasoning="想了一下"
        ),
    )
    assert [m for m in session.messages if m.role == "assistant"] == []


def test_an_empty_step_still_shows_the_thinking_it_did(tmp_path):
    """A6 T3: the model did think, and that is worth seeing before the reason.

    M7 changed the count, not the intent. A step that only thought now gets one
    chance to finish its answer before the turn ends, so the thinking can appear
    twice — and this fixture hands back the same step both times, which is an
    artifact of the fake. What A6 T3 asks is that the thinking is *shown*; a
    retry that shows it again does not violate that.
    """
    from ai_coding.core.events import AssistantMessage
    from ai_coding.domain.loop import StepResult

    _session, observer = _turn_with(
        tmp_path,
        StepResult(
            text=None, tool_calls=(), finish_reason="stop", usage=None, reasoning="想了一下"
        ),
    )
    shown = [m.reasoning for m in observer.events if isinstance(m, AssistantMessage)]
    assert shown, "the step's thinking was not shown at all"
    assert set(shown) == {"想了一下"}


def test_a_tool_only_step_is_not_an_empty_step(tmp_path):
    """A6 T6, the line this feature stands on: `text=None` is normal for a step
    that only calls tools.  Judging emptiness on text alone would kill every
    tool step in the product."""
    from ai_coding.domain.loop import StepResult, TurnStatus
    from ai_coding.domain.message import ToolCallRef

    session, observer = _turn_with(
        tmp_path,
        StepResult(
            text=None,
            tool_calls=(ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}'),),
            finish_reason="tool_calls",
            usage=None,
            reasoning=None,
        ),
    )
    outcome = _finished(observer).outcome
    assert outcome.status is not TurnStatus.ERROR or "没有回答" not in outcome.reason
    assert [m for m in session.messages if m.role == "assistant"] != []


def test_an_answered_step_is_unaffected(tmp_path):
    """A6 T5: the ordinary turn still completes."""
    from ai_coding.domain.loop import StepResult, TurnStatus

    _session, observer = _turn_with(
        tmp_path,
        StepResult(text="ok", tool_calls=(), finish_reason="stop", usage=None, reasoning=None),
    )
    outcome = _finished(observer).outcome
    assert outcome.status is TurnStatus.COMPLETED
    assert "没有回答" not in outcome.reason
