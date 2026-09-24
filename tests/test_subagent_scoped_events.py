"""TDD (R4a): a sub-agent's execution is no longer invisible.

The inner loop used to be built without an observer, so everything it did (which
tools, how long, how many tokens) was discarded.  Now its events reach the
parent's stream wrapped in ``ScopedEvent``, parented to the ``subAgent`` call
that spawned it — while its reasoning still never travels back (R1a-5).
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.core.events import RecordingObserver, ScopedEvent, ToolStarted
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _ReadTool(BaseTool):
    """A stand-in for the real read tool: enough to produce a child tool span."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "fake read"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        return ToolResult(success=True, output="hello")


class _Engine(ChatEngine):
    def __init__(self, steps) -> None:  # type: ignore[no-untyped-def]
        self._steps = list(steps)

    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        return self._steps.pop(0)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_subagent_events_reach_the_parent_observer_scoped(tmp_path):
    """The child runs one tool call, and the parent sees it under its call id."""
    steps = [
        StepResult(
            text=None,
            tool_calls=[ToolCallRef(id="child-1", name="read", arguments="{}")],
            finish_reason="tool_calls",
        ),
        StepResult(text="child done", finish_reason="stop"),
    ]
    observer = RecordingObserver()

    def build_tools(_workdir: str | None) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(_ReadTool())
        return registry

    runner = SubAgentRunner(
        app_config=None,
        engine_factory=lambda: _Engine(list(steps)),
        build_tools=build_tools,
        timeout_seconds=5.0,
        workspace=str(tmp_path),
        observer=observer,
    )

    packet = await runner.run("read sample.txt", name="child", isolate=False, parent_span="mcall")

    assert packet.status in {"completed", "max_iterations"}
    scoped = [event for event in observer.events if isinstance(event, ScopedEvent)]
    assert scoped, "the child's events must reach the parent's observer"
    assert all(event.parent == "mcall" for event in scoped)
    assert all(event.scope.startswith("subagent:") for event in scoped)
    inner_tools = [e for e in scoped if isinstance(e.event, ToolStarted)]
    assert [e.event.tool_name for e in inner_tools] == ["read"]
