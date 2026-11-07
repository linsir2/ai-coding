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
from ai_coding.config.models import (
    AIConfig,
    AppConfig,
    MCPConfig,
    MCPServerConfig,
    MCPToolRules,
)
from ai_coding.core.events import Degraded, RecordingObserver, ScopedEvent
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.domain.model_config import ModelConfig
from ai_coding.domain.subagent import ParentContext, SubAgentPoolFull, SubagentTask
from ai_coding.security.permission_gate import PermissionDecision
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
            yield SimpleNamespace(path=self._path, branch="ai_sub_test")

        return _cm(name)

    async def commit_if_dirty(self, path, message):
        return None, None


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
    write_coordinator=None,
    notices=None,
    notice_store=None,
    slots=None,
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
        write_coordinator=write_coordinator,
        notices=notices,
        notice_store=notice_store,
        slots=slots,
    )


def _parent(messages=("parent-said-this",)):
    return ParentContext(
        messages=tuple(
            ChatMessage(role="user", content=m, timestamp="t") for m in messages
        ),
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
async def test_subagent_does_not_inherit_the_parent_prompt(tmp_path):
    """r25/v2: only the parent's *messages* travel, never its assembled prompt."""
    engine = _Engine()
    await _runner(engine).run("t", parent=_parent())
    assert "PARENT INSTRUCTIONS" not in (engine.requests[0]["instructions"] or "")


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


# ---- SUB-05 (DEFERRED): the dispatcher gate stays on the main workspace ----


def _gate_runner(workspace, app: AppConfig | None = None) -> SubAgentRunner:
    return SubAgentRunner(
        app
        or AppConfig(
            default_model="m",
            models={"m": ModelConfig(name="m", base_url="https://x/v1", api_key="k")},
        ),
        engine_factory=lambda: _Engine(),
        build_tools=lambda _workdir: ToolRegistry(),
        workspace=str(workspace),
    )


def test_the_child_gate_does_not_switch_to_the_worktree(tmp_path):
    """Regression guard for the review finding: binding the gate to a worktree
    makes the main tree's "always" grants invisible (``GrantStore`` is keyed by
    workspace), so the gate must stay on the main workspace even for a child
    whose tools run inside a worktree.  The cost is documented in
    ``_child_gate`` and tracked as SUB-05 (deferred)."""
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    runner = _gate_runner(main)
    target = str(wt / "a.py")

    decision = runner._child_gate().check("read", {"path": target})

    assert "outside workspace" in decision.reason


def test_a_degraded_child_keeps_the_main_workspace_gate(tmp_path):
    """Regression guard: no workdir means the main tree, exactly as before."""
    main = tmp_path / "main"
    main.mkdir()
    runner = _gate_runner(main)

    decision = runner._child_gate().check(
        "read", {"path": str(main / "a.py")}
    )

    assert "outside workspace" not in decision.reason


def test_a_worktree_child_still_obeys_a_config_deny(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    app = AppConfig(
        default_model="m",
        models={"m": ModelConfig(name="m", base_url="https://x/v1", api_key="k")},
        mcp=MCPConfig(
            servers=[MCPServerConfig(name="srv", permissions=MCPToolRules(deny=["danger"]))]
        ),
    )
    runner = _gate_runner(main, app)

    decision = runner._child_gate().check("srv__danger", {})

    assert decision.decision is PermissionDecision.DENY


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
                                       max_tool_iterations=budget,
                                       # C-5b: a child is unbounded by default;
                                       # this test needs a bound to observe, so
                                       # it sets the child's own cap.
                                       subagent_max_tool_iterations=budget))

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
async def test_no_manager_means_no_degraded(tmp_path):
    """P2-A: "isolation was never asked for/configured" is not a degradation."""
    result = await _runner(_Engine()).run("t")
    assert result.degraded is None


@pytest.mark.asyncio
async def test_non_git_workspace_is_not_silently_shared(tmp_path):
    """P2-A: a configured manager that cannot isolate must say so.

    Running in the main workspace is still the behaviour (the child must not
    fail), but the result now carries ``worktree_unavailable`` and the scoped
    degraded event, so a write delegation cannot believe it was isolated.
    """

    class _NoGit:
        async def can_isolate(self) -> bool:
            return False

        def temporary(self, name: str):
            raise AssertionError("temporary() must not be entered")

    observer = RecordingObserver()
    seen: list = []
    result = await _runner(_Engine(), observer=observer, seen_tools=seen).run(
        "t", name="escaper", worktree_manager=_NoGit(), isolate=True,
        parent_span="call_1",
    )

    assert result.status == "completed"
    assert result.degraded == "worktree_unavailable"
    assert seen == [None]
    scoped = [
        event
        for event in observer.events
        if isinstance(event, ScopedEvent) and isinstance(event.event, Degraded)
    ]
    assert len(scoped) == 1
    assert scoped[0].event.component == "worktree_isolate"
    assert scoped[0].scope == "subagent:escaper:call_1"


@pytest.mark.asyncio
async def test_non_git_workspace_degrades_the_ledger_too(tmp_path):
    """P2-A: the ledger is written inside ``_run_turn``, before the non-git
    fallback is known and before any branch exists; the degradation must still
    be merged into the file (and the header/task that were already there kept).
    """

    class _NoGit:
        async def can_isolate(self) -> bool:
            return False

        def temporary(self, name: str):
            raise AssertionError("temporary() must not be entered")

    runner = _runner(_Engine(), ledger_root=tmp_path)
    packet = SubagentTask.from_objective("write a file")
    result = await runner.run(
        packet, name="escaper", worktree_manager=_NoGit(), isolate=True
    )

    assert result.degraded == "worktree_unavailable"
    assert result.ledger_path is not None
    payload = json.loads(Path(result.ledger_path).read_text(encoding="utf-8"))
    assert payload["degraded"] == "worktree_unavailable"
    # The refresh merges result keys; the header and task survive.
    assert payload["message_id"] == Path(result.ledger_path).stem
    assert payload["task"]["brief"]["objective"] == "write a file"


@pytest.mark.asyncio
async def test_request_is_persisted_in_ledger_and_meta(tmp_path):
    """The packet is the task the ledger names and the sidecar can rebuild."""
    runner = _runner(_Engine(), ledger_root=tmp_path)
    packet = SubagentTask.from_objective("do the thing", context_mode="task_only")
    run_id = runner.spawn_background(packet, name="probe", emit=lambda _e: None)
    row = None
    for _ in range(200):
        rows = [item for item in runner.task_list() if item.run_id == run_id]
        if rows and rows[0].status != "running":
            row = rows[0]
            break
        await asyncio.sleep(0.02)
    assert row is not None and row.ledger_path
    payload = json.loads(Path(row.ledger_path).read_text(encoding="utf-8"))
    assert payload["task"]["brief"]["objective"] == "do the thing"
    assert payload["run_id"] == run_id

    meta = json.loads(
        (tmp_path / ".ai-coding" / "agents" / "meta" / f"{run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert meta["taskPacket"]["brief"]["objective"] == "do the thing"


@pytest.mark.asyncio
async def test_timeout_still_writes_the_ledger_with_changes(tmp_path):
    """A cut keeps what the tools already declared (changed_paths + ledger)."""
    from ai_coding.domain.message import ToolArtifact

    class _HangSecond(_Engine):
        def __init__(self) -> None:
            super().__init__(
                [
                    StepResult(
                        tool_calls=[
                            ToolCallRef(id="c1", name="write", arguments='{"path":"a.py"}')
                        ],
                        finish_reason="tool_calls",
                    )
                ]
            )

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            if self.requests:
                await asyncio.sleep(30)
            return await super().step(
                messages, tools=tools, instructions=instructions, on_delta=on_delta
            )

    writer = _Tool(
        "write",
        ToolResult(
            success=True, output="wrote", artifacts=(ToolArtifact(path="a.py"),)
        ),
    )
    result = await _runner(
        _HangSecond(), registry=_registry(writer), timeout=0.05, ledger_root=tmp_path
    ).run("t")

    assert result.status == "timeout"
    assert result.ledger_path is not None
    assert result.changed_paths == ("a.py",)
    payload = json.loads(Path(result.ledger_path).read_text(encoding="utf-8"))
    assert payload["changed_paths"] == ["a.py"]
    assert payload["status"] == "timeout"


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


# ---- P2-B2: one admission set for sync, spawn and follow-up ---------------


@pytest.mark.asyncio
async def test_a_synchronous_run_occupies_one_of_the_slots(tmp_path):
    runner = _runner(_Engine(hang=True), max_concurrent=1)
    running = asyncio.create_task(runner.run("one"))
    for _ in range(200):
        if runner._active:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)

    assert runner._active, "a sync run must be admitted like any other"  # noqa: SLF001
    with pytest.raises(SubAgentPoolFull):
        runner.spawn_background("two", emit=lambda _e: None)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    for _ in range(200):
        if not runner._active:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    assert runner._active == set(), "finally must give the slot back"  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_start_failure_returns_the_slot(monkeypatch):
    runner = _runner(_Engine(), max_concurrent=1)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(runner, "_open_handle", _boom)
    with pytest.raises(RuntimeError, match="failed to start"):
        runner.spawn_background("two", emit=lambda _e: None)

    assert runner._active == set()  # noqa: SLF001


class _HangAfterFirst(_Engine):
    """The first turn settles; every later turn hangs until cancelled."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        if self.requests:
            await asyncio.sleep(30)
        return await super().step(
            messages, tools=tools, instructions=instructions, on_delta=on_delta
        )


@pytest.mark.asyncio
async def test_a_follow_up_turn_occupies_and_surrenders_a_slot(tmp_path):
    runner = _runner(_HangAfterFirst(), max_concurrent=1)
    run_id = runner.spawn_background("first", name="probe", emit=lambda _e: None)
    for _ in range(200):
        for row in runner.task_list():
            if row.run_id == run_id and row.status != "running":
                break
        else:
            await asyncio.sleep(0.02)
            continue
        break

    status, _ = await runner.send_message(run_id, "second")
    assert status == "running"
    assert run_id in runner._active  # noqa: SLF001

    await runner.interrupt(run_id)
    assert runner._active == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_finished_run_records_overlap_in_the_packet_and_ledger(tmp_path):
    """P2-B2: two runs that touched the same relative path are named, and the
    fact survives into the ledger (the parent's durable copy)."""
    from ai_coding.core.write_coordinator import WriteCoordinator
    from ai_coding.domain.message import ToolArtifact

    coordinator = WriteCoordinator(tmp_path)
    coordinator.register_run("other-run", (str(tmp_path / "a.py"),), "s1")
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
    runner = _runner(
        engine, registry=_registry(writer), ledger_root=tmp_path,
        write_coordinator=coordinator,
    )

    result = await runner.run("t")

    assert result.changed_paths == ("a.py",)
    assert result.conflicts == (
        {"key": "other-run", "origin_session": "s1", "paths": ("a.py",)},
    )
    assert result.ledger_path is not None
    payload = json.loads(Path(result.ledger_path).read_text(encoding="utf-8"))
    assert payload["conflicts"] == [
        {"key": "other-run", "origin_session": "s1", "paths": ["a.py"]}
    ]


# ---- P2-C1: the runner is one consumer of the shared pool ------------------


def _slot_runner(engine: _Engine, slots, *, registry: ToolRegistry | None = None):
    """A runner whose child stack shares the same ``slots`` pool."""
    base = registry or _registry()
    base.execution_slots = slots
    return _runner(engine, registry=base, slots=slots)


@pytest.mark.asyncio
async def test_slots_one_parent_and_child_do_not_deadlock(tmp_path):
    """The parent's tool-group slot is the child's slot; the child stack must
    see ``slot_held`` and run its own tools without asking again."""
    from ai_coding.core.execution_slots import ExecutionSlotPool
    from ai_coding.core.scheduler import Scheduler
    from ai_coding.domain.orchestration import TaskRequest

    slots = ExecutionSlotPool(1)
    tool = _Tool("read", ToolResult(success=True, output="ok"))
    engine = _Engine(
        [
            StepResult(
                tool_calls=[ToolCallRef(id="c1", name="read", arguments="{}")],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    runner = _slot_runner(engine, slots, registry=_registry(tool))
    scheduler = Scheduler(runner)
    request = TaskRequest.for_child(SubagentTask.from_objective("t"))

    result = await asyncio.wait_for(
        scheduler.run(request, parent=None, observer=None, emit=lambda _e: None),
        timeout=5,
    )

    assert result.status == "completed"
    assert tool.calls == [{}]
    assert slots.active == 0


@pytest.mark.asyncio
async def test_a_scheduler_run_releases_the_slot_on_a_cancelled_turn():
    from ai_coding.core.execution_slots import ExecutionSlotPool
    from ai_coding.core.scheduler import Scheduler
    from ai_coding.domain.orchestration import TaskRequest

    slots = ExecutionSlotPool(1)
    runner = _runner(_Engine(hang=True), max_concurrent=1)
    runner._slots = slots
    scheduler = Scheduler(runner)
    request = TaskRequest.for_child(SubagentTask.from_objective("t"))

    task = asyncio.create_task(
        scheduler.run(request, parent=None, observer=None, emit=None)
    )
    for _ in range(200):
        if slots.active:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.active == 0


@pytest.mark.asyncio
async def test_a_spawn_start_failure_does_not_leak_the_reserved_slot(monkeypatch):
    from ai_coding.core.execution_slots import ExecutionSlotPool
    from ai_coding.core.scheduler import Scheduler
    from ai_coding.domain.orchestration import TaskRequest

    slots = ExecutionSlotPool(1)
    runner = _runner(_Engine(), max_concurrent=1)
    runner._slots = slots
    scheduler = Scheduler(runner)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(runner, "_open_handle", _boom)
    request = TaskRequest.for_child(SubagentTask.from_objective("t"))
    with pytest.raises(RuntimeError, match="failed to start"):
        await scheduler.spawn(request, parent=None, observer=None, emit=None)
    assert slots.active == 0


@pytest.mark.asyncio
async def test_a_follow_up_interrupt_releases_the_slot():
    from ai_coding.core.execution_slots import ExecutionSlotPool

    slots = ExecutionSlotPool(1)
    runner = _runner(_HangAfterFirst(), max_concurrent=1)
    runner._slots = slots
    run_id = runner.spawn_background("first", name="probe", emit=lambda _e: None)
    for _ in range(200):
        row = next((item for item in runner.task_list() if item.run_id == run_id), None)
        if row is not None and row.status != "running":
            break
        await asyncio.sleep(0.02)

    assert slots.active == 0
    status, _ = await runner.send_message(run_id, "second")
    assert status == "running"
    assert slots.active == 1
    await runner.interrupt(run_id)
    assert slots.active == 0


@pytest.mark.asyncio
async def test_close_and_reap_release_the_same_slot():
    from ai_coding.core.execution_slots import ExecutionSlotPool

    slots = ExecutionSlotPool(1)
    runner = _runner(_Engine(hang=True), max_concurrent=1)
    runner._slots = slots
    run_id = runner.spawn_background("one", name="probe", emit=lambda _e: None)
    assert slots.active == 1
    await runner.close(run_id)
    assert slots.active == 0


@pytest.mark.asyncio
async def test_a_crashed_sync_child_returns_a_result_instead_of_raising(monkeypatch, tmp_path):
    """A crash used to escape ``_run_in``, so ``_run_isolated`` never reached
    ``_commit_turn`` and ``temporary()`` deleted every file the child wrote."""
    runner = _runner(_Engine())
    handle = SimpleNamespace(
        loop=None, context_mode="full", workdir=str(tmp_path), run_id="r1"
    )
    monkeypatch.setattr(runner, "_open_handle", lambda *a, **k: handle)

    async def boom(*args, **kwargs):
        raise RuntimeError("tool blew up")

    monkeypatch.setattr(runner, "_run_turn", boom)

    result = await runner._run_in("task", "probe", str(tmp_path), None)

    assert result.status == "error"
    assert "RuntimeError" in (result.error or "")
    assert result.exit_reason == "child_error"


@pytest.mark.asyncio
async def test_cancelling_a_child_has_a_grace_window(monkeypatch):
    """Cancellation is cooperative; a tool that ignores it must not hold the
    parent past the sub-agent's own timeout."""
    import time

    from ai_coding.core import subagent as module

    monkeypatch.setattr(module, "_CANCEL_GRACE_SECONDS", 0.1)
    stop = asyncio.Event()

    async def stubborn():
        while not stop.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    task.cancel()
    started = time.monotonic()
    await module._settle_cancelled(task)
    assert time.monotonic() - started < 1.0, "the grace window must bound the wait"
    # Let the abandoned child leave the loop instead of leaking into teardown.
    stop.set()
    task.cancel()
    await asyncio.sleep(0.1)

