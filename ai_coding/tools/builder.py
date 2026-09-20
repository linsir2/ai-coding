"""Tool stack builder (M4 refactor) — single place to assemble SDK tools.

Centralizes the per-config/per-directory tool assembly that used to live inline
in ``cli._build_tool_stack``.  Both the main agent and each sub-agent (via
``SubAgentRunner``'s injected builder) create their own stack through here, with
``include_subagent`` gating recursive sub-agent tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_coding.core.worktree import WorktreeManager
from ai_coding.tools.subagent_tool import SubAgentTool


def build_subagent_runner(app_cfg: Any, workspace: str, skills_path: str | None) -> Any:
    """Build a :class:`SubAgentRunner` bound to the given workspace.

    Its inner tool stacks resolve ``workdir=None`` to the main workspace and
    otherwise to the (worktree) directory actually passed in, always without a
    nested subAgent tool.  Sub-agent stacks use the non-interactive auto-approve
    policy (no human is present inside an isolated sub-agent to confirm WARN).
    """
    from ai_coding.core.subagent import SubAgentRunner

    main_ws = str(Path(workspace).expanduser().resolve())

    def build_tools(workdir: str | None) -> list[Any]:
        return build_tool_stack(
            app_cfg, workdir or main_ws, skills_path, include_subagent=False
        )

    return SubAgentRunner(app_cfg, build_service=_build_service, build_tools=build_tools)


def _build_service(app_cfg: Any, tools: list[Any] | None = None) -> Any:
    from ai_coding.ai.factory import build_service_from_config

    return build_service_from_config(app_cfg, tools=tools)


def build_tool_stack(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None = None,
    *,
    include_subagent: bool = True,
    approval: Any | None = None,
) -> list[Any]:
    """Build the SDK tool list for a configuration and directory.

    ``approval`` is the :class:`ApprovalCallback` used to resolve the WARN tier
    (write/edit/bash).  When ``None`` the stack falls back to a non-interactive
    auto-approve so the tools are usable in non-interactive entry points; the
    DENY tier (dangerous bash patterns) is always blocked regardless.
    """

    from ai_coding.security.approval import AutoApproveCallback
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.bash_tool import BashTool
    from ai_coding.tools.edit_tool import EditTool
    from ai_coding.tools.glob_tool import GlobTool
    from ai_coding.tools.read_tool import ReadTool
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.sdk_adapter import registry_to_sdk_tools
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
    registry.register(TodoWriteTool())

    if skills_path and Path(skills_path).is_dir():
        registry.register(SkillTool(skills_path))

    if include_subagent:
        # Real sub-agent delegation, isolated in a git worktree when enabled.
        runner = build_subagent_runner(app_cfg, workspace, skills_path)
        manager = WorktreeManager(workspace)
        isolate = bool(getattr(app_cfg.ai, "subagent_worktree_isolation", True))
        registry.register(
            SubAgentTool(runner=runner, worktree_manager=manager, isolate=isolate)
        )

    effective_approval = approval if approval is not None else AutoApproveCallback()
    return registry_to_sdk_tools(registry, gate, effective_approval)


__all__ = ["build_tool_stack", "build_subagent_runner"]
