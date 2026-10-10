"""TDD (R1a-5): SubAgentRunner — a sub-agent that shares the parent's context.

Rewritten from the M4 spec.  The old runner fed the sub-agent an EMPTY history
through the SDK service and got back a bare string; the new one runs the same
AgentLoop as the parent, seeded with the parent's request view and system prompt,
and returns a structured packet that is written back to a ledger.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_coding.ai.engine import ChatEngine, EngineError
from ai_coding.config.models import AIConfig
from ai_coding.core.events import Degraded, RecordingObserver, ScopedEvent
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.domain.subagent import ParentContext
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.todo_write_tool import TodoWriteTool


class _Probe:
    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    async def enter(self) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.02)
        self.current -= 1


class _Engine(ChatEngine):
    def __init__(self, steps=None, probe: _Probe | None = None, hang: bool = False):
        self._steps = list(steps or [StepResult(text="sub conclusion")])
        self._probe = probe
        self._hang = hang
        self.requests: list[dict] = []
        self.closed = False

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if self._probe is not None:
            await self._probe.enter()
        self.requests.append(
            {
                "messages": list(messages),
                "instructions": instructions,
                "tools": list(tools),
            }
        )
        if self._hang:
            await asyncio.sleep(30)
        item = self._steps.pop(0) if self._steps else StepResult(text="ok")
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class _Tool(BaseTool):
    def __init__(self, name: str, result: ToolResult | None = None) -> None:
        self._name = name
        self._result = result or ToolResult(success=True, output="ok")
        self.calls: list[dict] = []

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
        self.calls.append(params)
        return self._result


class _Worktree:
    def __init__(self, path: str = "wt/path", fail: bool = False) -> None:
        self._path = path
        self._fail = fail

    async def can_isolate(self) -> bool:
        return True

    def temporary(self, name: str):
        @asynccontextmanager
        async def _cm(_name: str):
            if self._fail:
                raise WorktreeError("boom")
            yield SimpleNamespace(path=self._path)

        return _cm(name)


def _registry(*extra: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    provided = {tool.name for tool in extra}
    if "todo_write" not in provided:
        registry.register(TodoWriteTool())
    for tool in extra:
        registry.register(tool)
    return registry


def _runner(
    engine: _Engine,
    *,
    registry: ToolRegistry | None = None,
    seen_tools: list | None = None,
    ledger_root=None,
    max_concurrent: int = 3,
    timeout: float = 600,
    settings: AIConfig | None = None,
    observer=None,
) -> SubAgentRunner:
    seen = seen_tools if seen_tools is not None else []

    def build_tools(workdir):
        seen.append(workdir)
        return registry or _registry()

    return SubAgentRunner(
        object(),
        engine_factory=lambda: engine,
        build_tools=build_tools,
        ledger_root=ledger_root,
        max_concurrent=max_concurrent,
        timeout_seconds=timeout,
        settings=settings or AIConfig(stop_policy="natural"),
        observer=observer,
    )


def _parent(messages=("parent-said-this",), instructions="PARENT INSTRUCTIONS"):
    return ParentContext(
        messages=tuple(
            ChatMessage(role="user", content=m, timestamp="t") for m in messages
        ),
        instructions=instructions,
    )


# ---- context sharing -------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_receives_parent_messages(tmp_path):
    engine = _Engine()
    await _runner(engine).run("do the task", parent=_parent())

    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert any("parent-said-this" in c for c in contents)
    assert any("do the task" in c for c in contents)
    # exactly one task block: the sub-agent's own (the parent view is injected
    # before the parent's own task block, so it must not bring one along)
    assert sum("<task-state>" in c for c in contents) == 1


@pytest.mark.asyncio
async def test_subagent_receives_parent_instructions(tmp_path):
    engine = _Engine()
    await _runner(engine).run("t", parent=_parent())
    assert engine.requests[0]["instructions"] == "PARENT INSTRUCTIONS"


@pytest.mark.asyncio
async def test_subagent_without_parent_uses_empty_history(tmp_path):
    engine = _Engine()
    await _runner(engine).run("t")
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert not any("parent-said-this" in c for c in contents)
    assert engine.requests[0]["instructions"] is None


# ---- structured packet -----------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_returns_structured_packet(tmp_path):
    engine = _Engine([StepResult(text="sub conclusion", finish_reason="stop")])
    result = await _runner(engine).run("t", parent=_parent())

    assert result.status == "completed"
    assert result.conclusion == "sub conclusion"
    assert result.text == "sub conclusion"  # backwards-compatible alias
    assert result.iterations == 1
    assert result.error is None


@pytest.mark.asyncio
async def test_subagent_todos_reported_in_packet(tmp_path):
    engine = _Engine(
        [
            StepResult(
                tool_calls=[
                    ToolCallRef(
                        id="c1",
                        name="todo_write",
                        arguments='{"action":"add","content":"ship it"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    result = await _runner(engine).run("t", parent=_parent())

    assert [entry["content"] for entry in result.todos] == ["ship it"]


@pytest.mark.asyncio
async def test_subagent_collects_artifacts(tmp_path):
    from ai_coding.domain.message import ToolArtifact

    writer = _Tool(
        "write",
        ToolResult(success=True, output="wrote", artifacts=(ToolArtifact(path="a.py"),)),
    )
    engine = _Engine(
        [
            StepResult(
                tool_calls=[
                    ToolCallRef(id="c1", name="write", arguments='{"path":"a.py"}')
                ],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    result = await _runner(engine, registry=_registry(writer)).run("t", parent=_parent())

    assert [a.path for a in result.artifacts] == ["a.py"]


@pytest.mark.asyncio
async def test_subagent_conclusion_truncated(tmp_path):
    settings = AIConfig(stop_policy="natural", max_subagent_conclusion_chars=10)
    engine = _Engine([StepResult(text="x" * 500, finish_reason="stop")])
    result = await _runner(engine, settings=settings).run("t")
    assert result.conclusion is not None
    assert len(result.conclusion) <= 11
    assert result.conclusion.endswith("…")


# ---- ledger ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_ledger_written(tmp_path):
    engine = _Engine([StepResult(text="ledgered", finish_reason="stop")])
    result = await _runner(engine, ledger_root=tmp_path).run("t")

    assert result.ledger_path is not None
    ledger = Path(result.ledger_path)
    assert ledger.is_file()
    assert ledger.parent == tmp_path / "transcripts" / "subagents"
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    # the ledger holds the FULL conclusion, not the truncated one
    assert payload["conclusion"] == "ledgered"


@pytest.mark.asyncio
async def test_subagent_ledger_failure_degrades(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    engine = _Engine()

    result = await _runner(engine, ledger_root=blocked).run("t")

    assert result.status == "completed"
    assert result.ledger_path is None


# ---- outcomes --------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_timeout_returns_timeout_status(tmp_path):
    engine = _Engine(hang=True)
    result = await _runner(engine, timeout=0.05).run("t")
    assert result.status == "timeout"
    assert engine.closed is True  # the engine is always released


# ---- R2a: a sub-agent is bounded too ---------------------------------------
# The parent used ``asyncio.wait_for`` around the sub-agent's turn, which cannot
# bound a tool that ignores cancellation.  Now the sub-turn carries its own
# deadline and the outer wait is only a backstop (docs/r2-analysis.md section 6.8).


@pytest.mark.asyncio
async def test_subagent_timeout_packet_carries_a_reason(tmp_path):
    engine = _Engine(hang=True)
    result = await _runner(engine, timeout=0.05).run("t")
    assert result.status == "timeout"
    assert result.error  # the parent must learn *why*, not just "timeout"


@pytest.mark.asyncio
async def test_subagent_turn_deadline_stops_it_before_the_outer_wait(tmp_path):
    """The inner loop's own ceiling fires first and produces a clean packet."""
    engine = _Engine(hang=True)
    settings = AIConfig(stop_policy="natural", turn_timeout_seconds=0.1)
    result = await _runner(engine, timeout=30, settings=settings).run("t")

    assert result.status == "timeout"
    assert result.error is not None and "deadline" in result.error


@pytest.mark.asyncio
async def test_stuck_tool_in_a_subagent_returns_a_structured_timeout(tmp_path):
    """A hanging tool must not hang the sub-agent (nor the parent)."""

    class _HangingTool(BaseTool):
        # A declared ceiling (like bash/subAgent have) so the turn keeps time to
        # recover; a tool without one legitimately consumes the whole budget.
        max_duration_seconds = 0.1

        def __init__(self) -> None:
            self.calls = 0

        @property
        def name(self) -> str:
            return "reader"

        @property
        def description(self) -> str:
            return "hangs"

        @property
        def params_json_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, params, ctx=None):  # type: ignore[override]
            self.calls += 1
            await asyncio.sleep(30)
            return ToolResult(success=True, output="never")

    tool = _HangingTool()
    engine = _Engine(
        [
            StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="reader", arguments="{}")],
                finish_reason="tool_calls",
            ),
            StepResult(text="recovered"),
        ]
    )
    settings = AIConfig(stop_policy="natural", turn_timeout_seconds=0.4)
    result = await _runner(
        engine, registry=_registry(tool), timeout=30, settings=settings
    ).run("t")

    assert result.status == "completed"
    assert result.conclusion == "recovered"
    second_request = engine.requests[1]["messages"]
    assert any(message.role == "tool" for message in second_request)


@pytest.mark.asyncio
async def test_subagent_engine_error_returns_error_status(tmp_path):
    engine = _Engine([EngineError("boom")])
    result = await _runner(engine).run("t")
    assert result.status == "error"
    assert result.error is not None and "boom" in result.error


@pytest.mark.asyncio
async def test_subagent_auto_approves_warn_tier_tools(tmp_path):
    """No human is present in a sub-agent: WARN tier must not be fail-closed."""
    writer = _Tool("write")
    engine = _Engine(
        [
            StepResult(
                tool_calls=[
                    ToolCallRef(id="c1", name="write", arguments='{"path":"a.py"}')
                ],
                finish_reason="tool_calls",
            ),
            StepResult(text="wrote it", finish_reason="stop"),
        ]
    )
    result = await _runner(engine, registry=_registry(writer)).run("t", parent=_parent())

    assert writer.calls == [{"path": "a.py"}]  # the write actually ran
    assert result.status == "completed"


# ---- isolation and concurrency --------------------------------------------


@pytest.mark.asyncio
async def test_subagent_respects_worktree_isolation(tmp_path):
    seen: list = []
    engine = _Engine()
    result = await _runner(engine, seen_tools=seen).run(
        "t", worktree_manager=_Worktree(), isolate=True
    )
    assert seen == ["wt/path"]
    assert result.workdir == "wt/path"


@pytest.mark.asyncio
async def test_subagent_worktree_failure_degrades(tmp_path):
    seen: list = []
    engine = _Engine()
    result = await _runner(engine, seen_tools=seen).run(
        "t", worktree_manager=_Worktree(fail=True), isolate=True
    )
    assert seen == [None]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_subagent_semaphore_bounds_concurrency(tmp_path):
    probe = _Probe()
    runner = _runner(_Engine(probe=probe), max_concurrent=1)
    await asyncio.gather(runner.run("one"), runner.run("two"))
    assert probe.peak == 1


@pytest.mark.asyncio
async def test_subagent_sub_loop_has_no_subagent_tool(tmp_path):
    """The provided stack is used verbatim: no nested delegation."""
    engine = _Engine()
    registry = _registry()
    await _runner(engine, registry=registry).run("t")
    names = [s["function"]["name"] for s in _schemas(engine)]
    assert "subAgent" not in names
    assert "todo_write" in names


def _schemas(engine: _Engine) -> list[dict]:
    """Schemas the sub-loop advertised to the engine."""
    return engine.requests[0]["tools"]


@pytest.mark.asyncio
async def test_subagent_does_not_auto_continue_its_budget():
    """Review fix: C3 must not multiply a sub-agent's iteration budget.

    A sub-agent is waited on by its parent and already carries its own
    wall-clock ceiling; letting it start three more rounds after its budget
    dies turns one delegated task into up to 4x the iterations.  Driven
    behaviourally rather than through the settings object: the child writes a
    todo item (so open_items is non-empty — the trigger C3 reads) and then
    burns the rest of its budget calling a tool that changes nothing.
    """
    budget = 3
    burn = [
        StepResult(
            tool_calls=[ToolCallRef(id=f"b{i}", name="noop", arguments="{}")],
            finish_reason="tool_calls",
        )
        for i in range(12)
    ]
    engine = _Engine([
        StepResult(
            tool_calls=[
                ToolCallRef(
                    id="c1",
                    name="todo_write",
                    arguments='{"action":"add","content":"ship it"}',
                )
            ],
            finish_reason="tool_calls",
        ),
        *burn,
    ])
    runner = _runner(engine, registry=_registry(_Tool("noop")),
                     settings=AIConfig(stop_policy="natural",
                                       max_tool_iterations=budget))

    await runner.run("t", parent=_parent())

    # todo_write plus the rest of the child's own budget, and not one round more
    assert len(engine.requests) <= 1 + budget
    assert not any(
        "[continue]" in (m.content or "")
        for request in engine.requests for m in request["messages"]
    )


@pytest.mark.asyncio
async def test_worktree_failure_names_the_subagent_that_escaped_isolation(tmp_path):
    """A child that loses its worktree runs in the *main* workspace instead.

    `report_degraded` carries no name of its own, so the only thing tying that
    record to a child is the scope wrapper every other one of its events goes
    through — and up to three sub-agents run at once, so an unscoped event
    cannot say which one wrote to the real tree.

    `worktree_isolate` has never fired in the recorded sessions (0 across every
    trace on disk), so this pins the shape, not a failure we have watched happen.
    """
    observer = RecordingObserver()
    await _runner(_Engine(), observer=observer).run(
        "t",
        name="escaper",
        worktree_manager=_Worktree(fail=True),
        isolate=True,
        parent_span="call_1",
    )

    scoped = [
        event
        for event in observer.events
        if isinstance(event, ScopedEvent) and isinstance(event.event, Degraded)
    ]
    assert len(scoped) == 1, [type(e).__name__ for e in observer.events]
    assert scoped[0].event.component == "worktree_isolate"
    assert scoped[0].scope == "subagent:escaper:call_1"
    assert scoped[0].parent == "call_1"


@pytest.mark.asyncio
async def test_worktree_failure_stays_silent_without_an_observer(tmp_path):
    """`_scoped_observer` returns None for no inner observer, and
    `report_degraded` returns early on None — the fallback must not need one."""
    result = await _runner(_Engine()).run(
        "t", worktree_manager=_Worktree(fail=True), isolate=True
    )

    assert result.status == "completed"

@pytest.mark.asyncio
async def test_resolve_run_id_matches_a_unique_prefix():
    """``/tasks`` prints run_id[:12]; ``stop`` must accept what the operator can
    actually copy — the same requirement the retired picker had."""
    runner = _runner(_Engine())
    run_id = runner.spawn_background("t", name="bg")
    assert runner.resolve_run_id(run_id) == run_id
    assert runner.resolve_run_id(run_id[:12]) == run_id
    assert runner.resolve_run_id("zzz") is None
