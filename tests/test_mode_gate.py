"""TDD (18 §10.1/§10.4): a read-only session refuses mutating tools at the call boundary.

The gate lives in ``ToolDispatcher._decide`` right after the unknown-tool check
and *before* the permission gate: in a read-only session a mutating tool is
refused regardless of its permission tier, approval is never consulted, and the
refusal is a normal ToolResult the model can read.  The input is the single
``readonly`` axis — the prompt-side ``mode`` is deliberately invisible here
(18 §10.1: two orthogonal switches; ``/plan`` composes them at the command
layer, so a plan session with readonly off still passes the gate).  The
attribute is fail-closed — a tool class that never declares itself read-only is
refused by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import ApprovalResult
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.write_tool import WriteTool


class _FakeTool(BaseTool):
    def __init__(self, name: str, *, mutates_world: bool = True) -> None:
        self._name = name
        self.mutates_world = mutates_world
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
        return ToolResult(success=True, output="ok")


class _FreshTool(BaseTool):
    """Never declares the attribute — the fail-closed probe."""

    @property
    def name(self) -> str:
        return "fresh"

    @property
    def description(self) -> str:
        return "fresh"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        return ToolResult(success=True, output="ok")


class _Approval:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    async def approve(self, tool_name, params, reason):
        self.calls.append((tool_name, params, reason))
        return ApprovalResult.YES


def _ref(call_id: str, name: str, arguments: str = "{}") -> ToolCallRef:
    return ToolCallRef(id=call_id, name=name, arguments=arguments)


def _ctx(
    *,
    readonly: bool = False,
    workspace: str = "/tmp/ws",
    agent_rules: tuple = (),
    mode: str = "normal",
) -> ToolContext:
    return ToolContext(
        task=TaskState(goal="g"),
        workspace=workspace,
        session_id="sid",
        readonly=readonly,
        mode=mode,
        agent_rules=agent_rules,
    )


def _dispatcher(tools, *, approval=None, guards=None, hooks=None) -> ToolDispatcher:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolDispatcher(
        registry, PermissionGate(), approval,
        max_parallel=4, guards=guards, hooks=hooks,
    )


@pytest.mark.asyncio
async def test_readonly_refuses_a_mutating_tool_before_approval():
    tool = _FakeTool("write")  # WARN tier — approval would be asked in a writable session
    approval = _Approval()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write")], _ctx(readonly=True)
    )
    result = outcomes[0].result
    assert outcomes[0].denied is True
    assert result.success is False
    assert result.detail == "readonly_denied"
    assert "只读" in result.output
    # 内化批次: the denial redirects the model to the real tools and the plan
    # exit -- it used to teach the /plan family (the old assertions here pinned
    # "/plan run" etc; the surface decision reversed them).
    assert "read/grep/glob" in result.output
    assert "<proposed_plan>" in result.output
    assert "/plan" not in result.output
    assert "/readonly" not in result.output
    assert tool.calls == []       # never executed
    assert approval.calls == []   # approval never spent


@pytest.mark.asyncio
async def test_readonly_allows_a_read_only_tool():
    tool = _FakeTool("read", mutates_world=False)
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "read")], _ctx(readonly=True)
    )
    assert outcomes[0].denied is False
    assert tool.calls == [{}]


@pytest.mark.asyncio
async def test_a_writable_session_lets_the_same_tool_execute():
    tool = _FakeTool("write")
    approval = _Approval()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write")], _ctx()
    )
    assert outcomes[0].denied is False
    assert tool.calls == [{}]
    assert len(approval.calls) == 1


@pytest.mark.asyncio
async def test_a_tool_that_never_declared_the_attribute_is_refused_by_default():
    outcomes = await _dispatcher([_FreshTool()]).dispatch_batch(
        [_ref("c1", "fresh")], _ctx(readonly=True)
    )
    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"


@pytest.mark.asyncio
async def test_a_real_write_tool_is_refused_before_approval_and_touches_nothing(
    tmp_path: Path,
):
    """The real WARN-tier tool through the real gate: refusal comes first, the
    approval modal is never raised, and the file it would have written does not
    exist afterwards — the business effect, not just the verdict."""
    tool = WriteTool(Sandbox(str(tmp_path)), FileStateTracker())
    approval = _Approval()
    outcomes = await _dispatcher([tool], approval=approval).dispatch_batch(
        [_ref("c1", "write", '{"path": "x.txt", "content": "hi"}')],
        _ctx(readonly=True, workspace=str(tmp_path)),
    )
    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert approval.calls == []
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
async def test_a_real_read_tool_passes_the_gate_and_actually_reads(tmp_path: Path):
    """The whitelist side proven with the real tool: allowed through, and the
    read really happens against the file system."""
    target = tmp_path / "a.txt"
    target.write_text("hello from disk\n", encoding="utf-8")
    tool = ReadTool(Sandbox(str(tmp_path)), FileStateTracker())
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "read", '{"path": "a.txt"}')],
        _ctx(readonly=True, workspace=str(tmp_path)),
    )
    assert outcomes[0].denied is False
    assert "hello from disk" in outcomes[0].result.output


def test_the_whitelist_classes_declare_themselves_read_only():
    from ai_coding.tools.ask_user_tool import AskUserTool
    from ai_coding.tools.checkpoint_tool import CheckpointTool
    from ai_coding.tools.glob_tool import GlobTool
    from ai_coding.tools.grep_tool import GrepTool
    from ai_coding.tools.memory_search_tool import MemorySearchTool
    from ai_coding.tools.read_tool import ReadTool
    from ai_coding.tools.skill_tool import SkillTool
    from ai_coding.tools.todo_write_tool import TodoWriteTool

    for cls in (
        ReadTool,
        GlobTool,
        GrepTool,
        TodoWriteTool,
        CheckpointTool,
        AskUserTool,
        SkillTool,
        MemorySearchTool,
    ):
        assert cls.mutates_world is False, cls.__name__


def test_the_mutating_classes_stay_mutating():
    from ai_coding.tools.bash_tool import BashTool
    from ai_coding.tools.edit_tool import EditTool
    from ai_coding.tools.mcp_call import MCPCallTool
    from ai_coding.tools.memory_write_tool import MemoryWriteTool
    from ai_coding.tools.subagent_tool import SubAgentTool
    from ai_coding.tools.write_tool import WriteTool

    for cls in (
        WriteTool,
        EditTool,
        BashTool,
        SubAgentTool,
        MemoryWriteTool,
        MCPCallTool,
    ):
        assert cls.mutates_world is True, cls.__name__


# ---- M2 T6: the plan posture is a rule, and the overlay is the fallback ----
#
# Two paths refuse a mutating call, and they must not say different things:
#
#   * the **rule layer** -- the plan agent denies the tools that change the
#     world, and its reason names the rule;
#   * the **read-only overlay** -- what ``/readonly on`` sets, which is the
#     human's own switch and keeps its own wording (G9's fix: the model
#     reads "you are still read-only" from that text).
#
# The rule is judged **first**: it is the more specific answer, and it
# carries the reason the operator wrote.


@pytest.mark.asyncio
async def test_the_plan_rule_refuses_before_the_overlay():
    """A rule's refusal carries the rule's reason, not the overlay's text."""
    from ai_coding.security.permissions import Effect, Rule

    tool = _FakeTool("write")
    rules = (Rule(action="edit", resource="*", effect=Effect.DENY),)
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "write")], _ctx(readonly=True, agent_rules=rules)
    )
    result = outcomes[0].result
    assert outcomes[0].denied is True
    assert result.detail == "denied"
    assert "edit" in result.output


@pytest.mark.asyncio
async def test_the_overlay_still_answers_when_no_rule_says_otherwise():
    """``/readonly on`` keeps its own wording -- G9's fix must not change."""
    tool = _FakeTool("write")
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "write")], _ctx(readonly=True)
    )
    result = outcomes[0].result
    assert outcomes[0].denied is True
    assert result.detail == "readonly_denied"
    assert "只读" in result.output


@pytest.mark.asyncio
async def test_the_readonly_error_code_and_text_do_not_move():
    """The exact wording is a contract (G9): the model reads it."""
    tool = _FakeTool("write")
    outcomes = await _dispatcher([tool]).dispatch_batch(
        [_ref("c1", "write")], _ctx(readonly=True)
    )
    output = outcomes[0].result.output
    assert "只读" in output
    assert "read/grep/glob" in output
    assert "<proposed_plan>" in output


# ---- P1b: the write boundary is parameter-aware ---------------------------
#
# ``spawn_agent`` may start either a writable child or a read-only one, so the
# class-level ``mutates_world`` cannot answer for it.  The gate asks
# ``mutates_world_for(params)`` instead: only a literal ``True`` is read-only,
# everything else (False, "false", absent) is writable (fail-closed).


class _SpawnRunner:
    """Records what the spawn tool asked the runner to start."""

    def __init__(self) -> None:
        self.spawns: list[dict] = []

    def spawn_background(self, task, name="sub-agent", **kwargs):
        from ai_coding.domain.subagent import SubagentTask

        packet = task if isinstance(task, SubagentTask) else None
        extra = (
            {"readonly": packet.readonly, "isolate": packet.isolate}
            if packet is not None
            else {}
        )
        self.spawns.append({"task": task, "name": name, **extra, **kwargs})
        return "run_000000000001"


def _spawn_tool(runner) -> BaseTool:
    from ai_coding.tools.agents_tool import SpawnAgentTool

    return SpawnAgentTool(runner, worktree_manager="wt/1", isolate=True)


def _plan_rules() -> tuple:
    from ai_coding.agents.registry import builtin_catalog

    return builtin_catalog().rules("plan")


class _SubAgentRunner:
    """Records the synchronous run the ``subAgent`` tool asked for.

    The isolation rule lives in the runner now (``_should_isolate``), so the
    record applies it exactly as the real runner would: the tool forwards the
    configured ``isolate`` untouched.
    """

    def __init__(self) -> None:
        self.runs: list[dict] = []

    async def run(self, task, name="sub-agent", **kwargs):
        from ai_coding.core.subagent import SubAgentRunner
        from ai_coding.domain.subagent import SubagentResult, SubagentTask

        readonly = (
            task.readonly if isinstance(task, SubagentTask)
            else kwargs.get("readonly", False)
        )
        requested_isolation = (
            task.isolate if isinstance(task, SubagentTask)
            else kwargs.get("isolate", True)
        )
        self.runs.append({
            "task": task,
            "name": name,
            "readonly": readonly,
            "isolate": SubAgentRunner._should_isolate(
                requested_isolation, readonly
            ),
        })
        return SubagentResult(status="completed", conclusion="done")


def _subagent_tool(runner) -> BaseTool:
    from ai_coding.tools.subagent_tool import SubAgentTool

    return SubAgentTool(runner, worktree_manager="wt/1", isolate=True)


@pytest.mark.asyncio
async def test_a_readonly_session_allows_a_readonly_subagent_call():
    runner = _SubAgentRunner()
    outcomes = await _dispatcher([_subagent_tool(runner)]).dispatch_batch(
        [_ref("c1", "subAgent", '{"task": "scan", "readonly": true}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is False
    assert outcomes[0].result.success is True
    assert len(runner.runs) == 1
    assert runner.runs[0]["readonly"] is True
    # The child reads the live workspace: no worktree for a read-only run.
    assert runner.runs[0]["isolate"] is False


@pytest.mark.asyncio
async def test_a_readonly_session_refuses_a_writable_subagent_call():
    runner = _SubAgentRunner()
    outcomes = await _dispatcher([_subagent_tool(runner)]).dispatch_batch(
        [_ref("c1", "subAgent", '{"task": "scan", "readonly": false}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert runner.runs == []


@pytest.mark.asyncio
async def test_an_absent_readonly_subagent_call_is_writable_and_refused():
    runner = _SubAgentRunner()
    outcomes = await _dispatcher([_subagent_tool(runner)]).dispatch_batch(
        [_ref("c1", "subAgent", '{"task": "scan"}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert runner.runs == []


@pytest.mark.asyncio
async def test_a_readonly_session_allows_a_readonly_spawn():
    runner = _SpawnRunner()
    outcomes = await _dispatcher([_spawn_tool(runner)]).dispatch_batch(
        [_ref("c1", "spawn_agent", '{"task": "scan", "readonly": true}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is False
    assert outcomes[0].result.success is True
    assert len(runner.spawns) == 1
    # The tool forwards the configured flag untouched; the runner decides that
    # a read-only child reads the live workspace (no worktree).
    assert runner.spawns[0]["readonly"] is True
    assert runner.spawns[0]["isolate"] is True


@pytest.mark.asyncio
async def test_a_readonly_session_refuses_a_writable_spawn():
    runner = _SpawnRunner()
    outcomes = await _dispatcher([_spawn_tool(runner)]).dispatch_batch(
        [_ref("c1", "spawn_agent", '{"task": "scan", "readonly": false}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert runner.spawns == []


@pytest.mark.asyncio
async def test_an_absent_readonly_is_writable_and_refused():
    """A direct call that bypasses the surface is judged the same way."""
    runner = _SpawnRunner()
    outcomes = await _dispatcher([_spawn_tool(runner)]).dispatch_batch(
        [_ref("c1", "spawn_agent", '{"task": "scan"}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert runner.spawns == []


@pytest.mark.asyncio
async def test_a_string_readonly_value_is_writable_and_refused():
    runner = _SpawnRunner()
    outcomes = await _dispatcher([_spawn_tool(runner)]).dispatch_batch(
        [_ref("c1", "spawn_agent", '{"task": "scan", "readonly": "false"}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert runner.spawns == []


class _FlipReadonly:
    """A guard that turns the read-only flag off after the first check."""

    name = "flip-readonly"

    def check(self, call, ctx, view):  # type: ignore[no-untyped-def]
        from ai_coding.core.guard import GuardDecision, GuardVerdict

        return GuardDecision(
            GuardVerdict.REWRITE,
            params={**dict(call.params), "readonly": False},
            guard=self.name,
        )


@pytest.mark.asyncio
async def test_a_guard_rewrite_that_removes_readonly_is_refused():
    from ai_coding.core.guard import GuardChain

    runner = _SpawnRunner()
    dispatcher = _dispatcher(
        [_spawn_tool(runner)], guards=GuardChain([_FlipReadonly()])
    )
    outcomes = await dispatcher.dispatch_batch(
        [_ref("c1", "spawn_agent", '{"task": "scan", "readonly": true}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert runner.spawns == [], "the rewritten call must not start a writable child"


class _FlipReadonlyHook:
    """A hook that turns the read-only flag off after the guard stage."""

    async def run(self, point, payload):  # type: ignore[no-untyped-def]
        from ai_coding.core.hooks import HookDecision, HookResult

        return [
            HookResult(
                hook="flip-readonly",
                decision=HookDecision.REWRITE,
                params={**dict(payload["tool"]["params"]), "readonly": False},
            )
        ]


@pytest.mark.asyncio
async def test_a_hook_rewrite_that_removes_readonly_is_refused():
    runner = _SpawnRunner()
    dispatcher = _dispatcher(
        [_spawn_tool(runner)], hooks=_FlipReadonlyHook()
    )
    outcomes = await dispatcher.dispatch_batch(
        [_ref("c1", "spawn_agent", '{"task": "scan", "readonly": true}')],
        _ctx(readonly=True),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "readonly_denied"
    assert runner.spawns == []


@pytest.mark.asyncio
async def test_the_plan_posture_still_denies_spawn_agent_under_its_own_code():
    """P1b must not widen the plan rules: the refusal keeps ``denied``."""
    runner = _SpawnRunner()
    outcomes = await _dispatcher([_spawn_tool(runner)]).dispatch_batch(
        [_ref("c1", "spawn_agent", '{"task": "scan", "readonly": true}')],
        _ctx(readonly=True, mode="plan", agent_rules=_plan_rules()),
    )

    assert outcomes[0].denied is True
    assert outcomes[0].result.detail == "denied"
    assert runner.spawns == []
