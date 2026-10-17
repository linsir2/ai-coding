"""TDD (R4a): the loop emits what the observability layer needs.

Nothing here changes what the loop *does* — it asserts that the loop now reports
what it did: request composition, real usage per step, the turn's totals, and a
``Degraded`` whenever an enhancement falls back (docs/r4-analysis.md 4.7 a6).
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import (
    ContextAssembled,
    Degraded,
    ModelStepFinished,
    RecordingObserver,
)
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.infra.deadline import DeadlineExpired
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _Tool(BaseTool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        return ToolResult(success=True, output="ok")


class _Engine(ChatEngine):
    def __init__(self, steps):  # type: ignore[no-untyped-def]
        self._steps = list(steps)
        self.requests: list[list] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        self.requests.append(list(messages))
        return self._steps.pop(0)

    async def aclose(self) -> None:
        return None


def _usage(prompt: int, completion: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _session() -> SessionData:
    return SessionData(
        session_id="s1", title="", created_time="2026-01-01T00:00:00Z", last_access_time=""
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Tool("read"))
    return registry


def _loop(engine, tmp_path, **kwargs) -> AgentLoop:
    kwargs.setdefault("settings", AIConfig(stop_policy="natural", max_tool_iterations=3))
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        _registry(),
        workspace=str(tmp_path),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_context_assembled_reports_composition(tmp_path):
    engine = _Engine([StepResult(text="done", finish_reason="stop", usage=_usage(100, 5))])
    observer = RecordingObserver()
    loop = _loop(engine, tmp_path, prompt=PromptAssembler("SYS" * 50), observer=observer)

    await loop.process_input(_session(), "hi")

    assembled = observer.of_type(ContextAssembled)
    assert len(assembled) == 1
    event = assembled[0]
    assert event.iteration == 1
    # 13 §2.3: the reporter counts the assembled system prompt — compare
    # against what the builder produces (methods included), not the raw base.
    # C-2: the baseline carries the request's tool surface (this loop's
    # registry holds one tool, "read").
    assert event.system_chars == len(PromptAssembler("SYS" * 50).build(tools=("read",)))
    assert event.tools_chars > 0  # the tool schema is part of the prompt
    assert event.history_messages >= 1
    assert event.history_chars > 0
    assert event.estimated_tokens > 0
    assert event.digest


@pytest.mark.asyncio
async def test_an_append_only_second_iteration_keeps_a_shared_prefix(tmp_path):
    engine = _Engine(
        [
            StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="read", arguments="{}")],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    observer = RecordingObserver()
    loop = _loop(engine, tmp_path, prompt=PromptAssembler("SYS" * 50), observer=observer)
    session = _session()
    # A realistic history: with only one message the volatile tail (task block)
    # dominates the request, so the prefix share is meaningless.
    for index in range(6):
        session.add_message(
            ChatMessage(
                role="user" if index % 2 == 0 else "assistant",
                content="padding " * 6,
                timestamp="",
            )
        )

    await loop.process_input(session, "hi")

    assembled = observer.of_type(ContextAssembled)
    assert len(assembled) == 2
    assert assembled[0].prefix_shared_ratio == 0.0  # nothing before the first
    assert assembled[1].prefix_shared_ratio > 0.5  # appended, not rewritten
    # The volatile tail is what caps the share: everything before the *previous*
    # tail is still a stable prefix, which is exactly why it lives at the end.
    assert assembled[1].prefix_shared_chars > assembled[1].system_chars


@pytest.mark.asyncio
async def test_model_step_finished_reports_usage_latency_and_cache(tmp_path):
    step = StepResult(
        text="done",
        finish_reason="stop",
        usage={**_usage(100, 5), "cached_tokens": 80},
    )
    observer = RecordingObserver()
    loop = _loop(_Engine([step]), tmp_path, observer=observer)

    outcome_text = await loop.process_input(_session(), "hi")

    assert outcome_text == "done"
    finished = observer.of_type(ModelStepFinished)
    assert len(finished) == 1
    assert finished[0].usage == step.usage
    assert finished[0].cached_tokens == 80
    assert finished[0].finish_reason == "stop"
    assert finished[0].text_chars == len("done")


@pytest.mark.asyncio
async def test_turn_outcome_carries_the_spend(tmp_path):
    engine = _Engine(
        [
            StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="read", arguments="{}")],
                finish_reason="tool_calls",
                usage=_usage(100, 10),
            ),
            StepResult(text="done", finish_reason="stop", usage=_usage(200, 20)),
        ]
    )
    loop = _loop(engine, tmp_path)

    await loop.process_input(_session(), "hi")

    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED
    assert loop.last_outcome.usage == {
        "prompt_tokens": 300,
        "completion_tokens": 30,
        "total_tokens": 330,
        # 19 §3.5: the window reading is the LAST step's prompt (200), not the sum.
        "context_tokens": 200,
    }


@pytest.mark.asyncio
async def test_a_failing_memory_block_is_visible_not_silent(tmp_path):
    class BrokenMemory:
        def index_notes(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("index unreadable")

        def render_index_block(self, notes):  # type: ignore[no-untyped-def]
            return ""

        def status(self):  # type: ignore[no-untyped-def]
            return {}

        async def harvest(self, *a, **k):  # type: ignore[no-untyped-def]
            return None

    observer = RecordingObserver()
    loop = _loop(
        _Engine([StepResult(text="done", finish_reason="stop")]),
        tmp_path,
        memory=BrokenMemory(),
        observer=observer,
    )

    await loop.process_input(_session(), "hi")

    components = [event.component for event in observer.of_type(Degraded)]
    assert "memory_block" in components


@pytest.mark.asyncio
async def test_a_failing_compaction_is_visible_and_falls_back(tmp_path):
    class TimingOutContext:
        async def compress_if_needed(self, history, on_summary=None):  # type: ignore[no-untyped-def]
            raise DeadlineExpired("context_compress", 0.01)

    observer = RecordingObserver()
    loop = _loop(
        _Engine([StepResult(text="done", finish_reason="stop")]),
        tmp_path,
        context=TimingOutContext(),
        observer=observer,
    )

    await loop.process_input(_session(), "hi")

    components = [event.component for event in observer.of_type(Degraded)]
    assert "context_compress" in components


@pytest.mark.asyncio
async def test_a_failed_persist_is_visible_but_does_not_end_the_turn(tmp_path):
    class BrokenSessions(SessionService):
        def save(self, session):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        def mark_pending_approval(self, session_id, request):  # type: ignore[no-untyped-def]
            """P3: the marker is best-effort too -- a full disk skips it."""
            return None

    observer = RecordingObserver()
    loop = AgentLoop(
        _Engine([StepResult(text="done", finish_reason="stop")]),
        BrokenSessions(tmp_path / "sessions"),
        _registry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
        observer=observer,
    )

    text = await loop.process_input(_session(), "hi")

    assert text == "done"
    components = [event.component for event in observer.of_type(Degraded)]
    assert "persist" in components
