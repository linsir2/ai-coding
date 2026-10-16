"""TDD: builder — assemble the tool registry for the main and sub agents.

Rewritten in R1a-6: the SDK-shaped ``build_tool_stack`` is gone (nothing used it
once sub-agents moved onto the self-owned loop), so this file now covers only the
registry builder and the sub-agent runner wiring.
"""

from types import SimpleNamespace

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
    """A sub-agent runs on a throwaway session (plan_runner / subagent both
    build a fresh SessionData), so its own transcript is three messages long and
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
    assert "subAgent" not in names  # no recursion
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
    assert runner._semaphore._value == 2  # asyncio.Semaphore exposes its count


def test_r9_the_dependency_gate_is_on_by_default_and_configurable(tmp_path):
    on = _cfg(str(tmp_path))  # _cfg has no enforce_dependencies → getattr default True
    assert build_tool_registry(on, str(tmp_path), None).get("todo_write")._enforce is True

    off = _cfg(str(tmp_path))
    off.ai.enforce_dependencies = False
    assert build_tool_registry(off, str(tmp_path), None).get("todo_write")._enforce is False
