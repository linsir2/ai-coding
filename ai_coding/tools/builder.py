"""Tool stack builder — the single place tools get assembled.

Both the main agent and each sub-agent (through ``SubAgentRunner``'s injected
builder) get their stack from here; ``include_subagent`` gates recursive
delegation.  The registry holds tools only — the permission gate and the approval
callback belong to the ``ToolDispatcher`` the loop builds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_coding.config.models import AIConfig
from ai_coding.core.worktree import WorktreeManager
from ai_coding.tools.subagent_tool import SubAgentTool


def build_subagent_runner(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    *,
    base_instructions: str = "",
) -> Any:
    """Build a :class:`SubAgentRunner` bound to the given workspace.

    A sub-agent runs the same loop as its parent, so the runner needs an engine
    factory plus a *registry* of tools — always without a nested ``subAgent``, or
    delegation would recurse.  Its tool stacks resolve ``workdir=None`` to the
    main workspace and otherwise to the worktree directory passed in.
    """
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.core.subagent import SubAgentRunner
    from ai_coding.security.approval import AutoApproveCallback

    main_ws = str(Path(workspace).expanduser().resolve())

    def build_tools(workdir: str | None) -> Any:
        return build_tool_registry(
            app_cfg, workdir or main_ws, skills_path, include_subagent=False
        )

    ai_cfg = getattr(app_cfg, "ai", None)
    return SubAgentRunner(
        app_cfg,
        engine_factory=lambda: build_engine_from_config(app_cfg),
        build_tools=build_tools,
        ledger_root=main_ws,
        max_concurrent=int(getattr(ai_cfg, "max_concurrent_subagents", 3)),
        timeout_seconds=float(getattr(ai_cfg, "subagent_timeout_seconds", 600)),
        settings=ai_cfg if isinstance(ai_cfg, AIConfig) else AIConfig(),
        # No human is present inside an isolated sub-agent to confirm WARN tier;
        # the DENY tier (dangerous bash) still blocks regardless.
        approval=AutoApproveCallback(),
        workspace=main_ws,
        # R7e-4: a task_only child gets this instead of the parent's prompt.
        base_instructions=base_instructions,
    )


def build_tool_registry(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None = None,
    *,
    include_subagent: bool = True,
    user_prompt: Any | None = None,
    base_instructions: str = "",
) -> Any:
    """Build the *domain* tool registry used by the self-owned loop (R1a-4).

    The registry holds tools only: the permission gate and the approval callback
    belong to the ``ToolDispatcher``, which the loop builds.

    R6c: ``user_prompt`` is the channel ``ask_user`` would use.  A sub-agent stack
    (``include_subagent=False``) never gets it: delegating to an isolated worktree
    means there is nobody to ask, so the tool list must not pretend otherwise.
    """
    registry, _gate = _assemble_registry(
        app_cfg, workspace, skills_path, include_subagent, user_prompt, base_instructions
    )
    return registry


def _assemble_registry(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    include_subagent: bool,
    user_prompt: Any | None = None,
    base_instructions: str = "",
) -> tuple[Any, Any]:
    """Create the sandbox/gate/registry triple shared by both builders."""

    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.bash_tool import BashTool
    from ai_coding.tools.edit_tool import EditTool
    from ai_coding.tools.glob_tool import GlobTool
    from ai_coding.tools.read_tool import ReadTool
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.skill_tool import SkillTool
    from ai_coding.tools.todo_write_tool import TodoWriteTool
    from ai_coding.tools.write_tool import WriteTool

    sandbox = Sandbox(workspace)
    fst = FileStateTracker()
    gate = PermissionGate()
    registry = ToolRegistry()

    tools_cfg = app_cfg.tools

    if tools_cfg.bash.enabled:
        registry.register(BashTool(timeout_seconds=tools_cfg.bash.timeout_seconds))
    if tools_cfg.read.enabled:
        registry.register(
            ReadTool(sandbox, fst, max_file_size=tools_cfg.read.max_file_size)
        )
    if tools_cfg.write.enabled:
        registry.register(WriteTool(sandbox, fst))
    if tools_cfg.edit.enabled:
        registry.register(EditTool(sandbox, fst))
    if tools_cfg.glob.enabled:
        registry.register(GlobTool(sandbox))

    # Always-registered tools
    # R9a: the dependency gate is on by default; legacy lists can switch it off.
    # ``app_cfg.ai`` is duck-typed (tests pass namespaces), so read it defensively.
    registry.register(
        TodoWriteTool(
            enforce_dependencies=bool(getattr(app_cfg.ai, "enforce_dependencies", True))
        )
    )

    if skills_path and Path(skills_path).is_dir():
        registry.register(SkillTool(skills_path))

    # R6c: only where a human can answer — and never inside a sub-agent.  The tools
    # config is duck-typed (tests pass namespaces), so a missing block means "not
    # configured", not an error.
    ask_user_cfg = getattr(tools_cfg, "ask_user", None)
    if (
        include_subagent
        and user_prompt is not None
        and bool(getattr(ask_user_cfg, "enabled", False))
    ):
        from ai_coding.tools.ask_user_tool import AskUserTool

        registry.register(
            AskUserTool(
                user_prompt,
                timeout_seconds=float(getattr(ask_user_cfg, "timeout_seconds", 300)),
            )
        )

    if include_subagent:
        # Real sub-agent delegation, isolated in a git worktree when enabled.
        runner = build_subagent_runner(
            app_cfg, workspace, skills_path, base_instructions=base_instructions
        )
        manager = WorktreeManager(workspace)
        isolate = bool(getattr(app_cfg.ai, "subagent_worktree_isolation", True))
        registry.register(
            SubAgentTool(runner=runner, worktree_manager=manager, isolate=isolate)
        )

    return registry, gate


__all__ = ["build_tool_registry", "build_subagent_runner"]
