"""TDD: builder — assemble the tool registry for the main and sub agents.

Rewritten in R1a-6: the SDK-shaped ``build_tool_stack`` is gone (nothing used it
once sub-agents moved onto the self-owned loop), so this file now covers only the
registry builder and the sub-agent runner wiring.
"""

from types import SimpleNamespace

from ai_coding.core.agent_loop import AgentLoop
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.tools.builder import build_subagent_runner, build_tool_registry


def _cfg(ws: str) -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(
            bash=SimpleNamespace(enabled=True, timeout_seconds=60),
            read=SimpleNamespace(enabled=True, max_file_size=1 << 20),
            write=SimpleNamespace(enabled=True),
            edit=SimpleNamespace(enabled=True),
            glob=SimpleNamespace(enabled=True),
        ),
        ai=SimpleNamespace(subagent_worktree_isolation=True),
    )


def test_registry_builder_returns_tool_registry(tmp_path):
    registry = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    names = set(registry.list_names())
    assert {"bash", "read", "write", "edit", "glob", "todo_write"} <= names


def test_registry_builder_includes_subagent_by_default(tmp_path):
    registry = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    assert "subAgent" in registry.list_names()


def test_registry_builder_excludes_nested_subagent(tmp_path):
    registry = build_tool_registry(
        _cfg(str(tmp_path)), str(tmp_path), None, include_subagent=False
    )
    assert "subAgent" not in registry.list_names()
    assert "read" in registry.list_names()


def test_parallel_research_is_a_main_session_tool_only(tmp_path):
    """B0: it needs a runner; a child stack must not fan out again."""
    registry = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    assert "parallel_research" in registry.list_names()

    inner = build_tool_registry(
        _cfg(str(tmp_path)), str(tmp_path), None, include_subagent=False
    )
    assert "parallel_research" not in inner.list_names()


def test_registry_builder_skips_disabled_tools(tmp_path):
    cfg = _cfg(str(tmp_path))
    cfg.tools.read.enabled = False
    registry = build_tool_registry(cfg, str(tmp_path), None)
    assert "read" not in registry.list_names()
    assert "glob" in registry.list_names()


def test_registry_builder_adds_skill_tool_only_for_a_real_directory(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    registry = build_tool_registry(
        _cfg(str(tmp_path)), str(tmp_path), str(skills)
    )
    assert "skill" in registry.list_names()


def test_history_search_is_a_main_session_tool_only(tmp_path):
    """A sub-agent runs on a throwaway session (the subagent runner builds a
    fresh SessionData), so its own transcript is three messages long and
    a recall tool over it can only ever answer "you just said that". The schema
    is not free either -- this repo measures every wasted character per step.
    """
    inner = build_subagent_runner(_cfg(str(tmp_path)), str(tmp_path), None)._build_tools(None)
    assert "history_search" not in set(inner.list_names())

    main = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    assert "history_search" in main.list_names()


def test_subagent_runner_inner_stack_has_no_nested_subagent(tmp_path):
    runner = build_subagent_runner(_cfg(str(tmp_path)), str(tmp_path), None)
    inner = runner._build_tools(None)  # the sub-agent's own tools (main workdir)
    names = set(inner.list_names())
    # P2-B2: depth is structurally one -- neither the old name nor the new
    # Codex-shaped tools may appear in a child's stack.
    assert "subAgent" not in names  # no recursion
    assert "spawn_agent" not in names
    assert "read" in names


def test_subagent_runner_wires_ledger_root_and_limits(tmp_path):
    cfg = SimpleNamespace(
        tools=_cfg(str(tmp_path)).tools,
        ai=SimpleNamespace(
            subagent_worktree_isolation=True,
            max_concurrent_subagents=2,
            subagent_timeout_seconds=45,
        ),
    )
    runner = build_subagent_runner(cfg, str(tmp_path), None)
    assert runner._ledger_root == tmp_path.resolve()
    assert runner._timeout == 45.0
    assert runner.slots.max_slots == 2
    assert runner.max_concurrent == 2


def test_r9_the_dependency_gate_is_on_by_default_and_configurable(tmp_path):
    on = _cfg(str(tmp_path))  # _cfg has no enforce_dependencies → getattr default True
    assert build_tool_registry(on, str(tmp_path), None).get("todo_write")._enforce is True

    off = _cfg(str(tmp_path))
    off.ai.enforce_dependencies = False
    assert build_tool_registry(off, str(tmp_path), None).get("todo_write")._enforce is False


# ---- P1b: what a read-only / plan request may see --------------------------


def _surface(
    mode: str, readonly: bool, rules=(), loaded=(), *, include_deferred=False
) -> set[str]:
    loop = SimpleNamespace(
        tools=build_tool_registry(_cfg("."), ".", None),
        gate=PermissionGate(),
    )
    return {
        tool.name
        for tool in AgentLoop._surface_tools(
            loop, mode, readonly, (), tuple(rules), "", loaded, include_deferred
        )
    }


def test_a_readonly_request_sees_spawn_agent_but_must_load_subagent(tmp_path):
    names = _surface("normal", True)

    assert "spawn_agent" in names, "a read-only spawn is a legal call here"
    assert "subAgent" not in names, "still deferred: tool_search has to load it"

    loaded = _surface("normal", True, loaded=("subAgent",))
    assert "subAgent" in loaded, "loading is a surface change, not a permission"


def test_the_plan_request_shows_neither_spawn_name():
    from ai_coding.agents.registry import builtin_catalog

    rules = builtin_catalog().rules("plan")
    names = _surface("plan", True, rules, loaded=("subAgent",))

    assert "subAgent" not in names
    assert "spawn_agent" not in names


def test_the_tool_search_path_also_gets_neither_spawn_name_in_plan():
    """``include_deferred`` lifts only the cost filter: ``tool_search`` must
    not offer a tool whose every call the plan posture refuses."""
    from ai_coding.agents.registry import builtin_catalog

    rules = builtin_catalog().rules("plan")
    names = _surface("plan", True, rules, include_deferred=True)

    assert "subAgent" not in names
    assert "spawn_agent" not in names


def test_parent_and_child_registries_share_one_write_coordinator(tmp_path):
    """P2-B1: every writer — the parent and every child stack — must serialize
    against the same table, or two sub-agents could write the same file."""
    parent = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    runner = parent.scheduler.runner
    child = runner._build_tools(None)

    assert parent.write_coordinator is not None
    assert child.write_coordinator is parent.write_coordinator


def test_an_injected_write_coordinator_is_used_by_both_stacks(tmp_path):
    from ai_coding.core.write_coordinator import WriteCoordinator

    coordinator = WriteCoordinator(tmp_path)
    parent = build_tool_registry(
        _cfg(str(tmp_path)), str(tmp_path), None, write_coordinator=coordinator
    )
    child = parent.scheduler.runner._build_tools(None)

    assert parent.write_coordinator is coordinator
    assert child.write_coordinator is coordinator


def test_bash_tool_gets_the_same_coordinator(tmp_path):
    coordinator_holder = {}

    parent = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    coordinator_holder["c"] = parent.write_coordinator
    assert parent.get("bash")._write_coordinator is coordinator_holder["c"]


def test_goal_tools_are_main_stack_only(tmp_path):
    """D-4: the goal run is dispatched on the MAIN session; a sub-agent stack
    gets no start_goal/update_goal (arming a throwaway session's task would
    drive nothing)."""
    main = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    names = set(main.list_names())
    assert {"start_goal", "update_goal"} <= names

    sub = build_tool_registry(
        _cfg(str(tmp_path)), str(tmp_path), None, include_subagent=False
    )
    sub_names = set(sub.list_names())
    assert "start_goal" not in sub_names and "update_goal" not in sub_names


# ---- P2-C1: one pool and one scheduler shared by the whole stack -----------


def test_parent_and_child_stacks_share_one_slot_pool_and_scheduler(tmp_path):
    parent = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    runner = parent.scheduler.runner
    child = runner._build_tools(None)

    assert parent.execution_slots is runner.slots
    assert child.execution_slots is parent.execution_slots
    assert child.scheduler is parent.scheduler
    # The child keeps the same capacity but no orchestration tools.
    assert child.scheduler.runner is runner
    assert "subAgent" not in child.list_names()
    assert "spawn_agent" not in child.list_names()


def test_max_execution_slots_overrides_the_subagent_fallback(tmp_path):
    cfg = SimpleNamespace(
        tools=_cfg(str(tmp_path)).tools,
        ai=SimpleNamespace(
            subagent_worktree_isolation=True,
            max_concurrent_subagents=3,
            max_execution_slots=2,
            subagent_timeout_seconds=45,
        ),
    )
    parent = build_tool_registry(cfg, str(tmp_path), None)
    assert parent.execution_slots.max_slots == 2
    assert parent.scheduler.runner.max_concurrent == 2


def test_an_unset_max_execution_slots_falls_back_to_the_subagent_limit(tmp_path):
    cfg = SimpleNamespace(
        tools=_cfg(str(tmp_path)).tools,
        ai=SimpleNamespace(
            subagent_worktree_isolation=True,
            max_concurrent_subagents=3,
            max_execution_slots=0,
        ),
    )
    parent = build_tool_registry(cfg, str(tmp_path), None)
    assert parent.execution_slots.max_slots == 3



# ---- P2-C3: the merge queue is assembled once and stays on the main stack --


def test_main_stack_has_one_merge_queue_and_tool(tmp_path):
    parent = build_tool_registry(_cfg(str(tmp_path)), str(tmp_path), None)
    runner = parent.scheduler.runner
    child = runner._build_tools(None)

    assert parent.scheduler._merge_queue is not None
    assert parent.get("merge_agent") is not None
    assert "merge_agent" not in child.list_names()
    assert child.scheduler is parent.scheduler


def test_low_frequency_orchestration_tools_are_deferred():
    """R2-8: ~3.7k characters of rarely used agent-control schemas were resent
    on every step.  They stay reachable through ``tool_search``."""
    from ai_coding.tools.builder import DEFERRED_TOOL_NAMES

    for name in (
        "send_message",
        "interrupt_agent",
        "close_agent",
        "resume_agent",
        "merge_agent",
        "history_search",
    ):
        assert name in DEFERRED_TOOL_NAMES
    # The backbone of parallel work stays advertised: deferring it would cost
    # an extra round trip on the task it exists for.
    for name in ("spawn_agent", "wait_agent", "list_agents"):
        assert name not in DEFERRED_TOOL_NAMES
