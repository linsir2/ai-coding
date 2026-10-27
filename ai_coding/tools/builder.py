"""Tool stack builder — the single place tools get assembled.

Both the main agent and each sub-agent (through ``SubAgentRunner``'s injected
builder) get their stack from here; ``include_subagent`` gates recursive
delegation.  The registry holds tools only — the permission gate and the approval
callback belong to the ``ToolDispatcher`` the loop builds.
"""

from __future__ import annotations

import glob as _glob
from pathlib import Path
from typing import Any

from ai_coding.config.models import AIConfig
from ai_coding.core.worktree import WorktreeManager
from ai_coding.skills.registry import SkillRegistry
from ai_coding.tools.subagent_tool import SubAgentListTool, SubAgentTool

#: The characters that make a configured root a pattern rather than a path.
_WILDCARDS = "*?["


def expand_root_patterns(
    patterns: Any, *, base: Path | None = None
) -> tuple[Path, ...]:
    """Turn the configured extra roots into concrete directories (B).

    A root is a standing "always", so it must never re-ask.  Patterns are
    supported because the useful shape is a sibling repo tree
    (``/home/me/projects/*``), not one directory; only the *directory* part of a
    match is kept, because a file is not somewhere to run a search from.

    Three rules make this safe to feed a path into: an entry without a wildcard
    is resolved as-is (so ``~`` and relative paths still work), a pattern that
    matches nothing yields nothing rather than an error, and every result is
    resolved, so a symlinked root cannot smuggle in a path the sandbox would
    later refuse.

    ``base`` resolves a relative entry against the workspace instead of the
    process CWD -- the CWD is not the workspace when the app is launched from
    elsewhere, and a relative root silently following ``cd`` is the same class of
    bug the embedding cache used to have (paths.py docstring).
    """
    roots: list[Path] = []
    for raw in patterns or ():
        entry = str(raw or "").strip()
        if not entry:
            continue
        expanded = Path(entry).expanduser()
        if base is not None and not expanded.is_absolute():
            expanded = base / expanded
        if not any(char in entry for char in _WILDCARDS):
            candidates: list[Path] = [expanded]
        else:
            candidates = [Path(match) for match in sorted(_glob.glob(str(expanded)))]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
    return tuple(roots)


def build_subagent_runner(
    app_cfg: Any,
    workspace: str,
    skills: SkillRegistry | None,
    *,
    base_instructions: str = "",
    notices: Any | None = None,
    jobs: Any = None,
    agents: Any = None,
    sandbox_policy: Any = None,
    sandbox_backend: Any = None,
    sandbox_roots: Any = (),
) -> Any:
    """Build a :class:`SubAgentRunner` bound to the given workspace.

    A sub-agent runs the same loop as its parent, so the runner needs an engine
    factory plus a *registry* of tools — always without a nested ``subAgent``, or
    delegation would recurse.  Its tool stacks resolve ``workdir=None`` to the
    main workspace and otherwise to the worktree directory passed in.

    M-D1: the parent's process sandbox has to reach the child's ``BashTool`` too.
    A sub-agent could otherwise delegate a write to the workspace through
    ``subAgent`` and the model would bypass the boundary the parent's own bash is
    held to.  ``sandbox_policy``/``sandbox_backend`` are therefore threaded
    through, and the writable ``roots`` follow the child: when it runs in a
    worktree the roots are that worktree, otherwise the caller's own roots.
    """
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.core.subagent import SubAgentRunner
    from ai_coding.security.approval import AutoApproveCallback

    main_ws = str(Path(workspace).expanduser().resolve())

    def build_tools(workdir: str | None) -> Any:
        roots = tuple(sandbox_roots)
        if workdir:
            roots = (Path(workdir).expanduser().resolve(),)
        return build_tool_registry(
            app_cfg,
            workdir or main_ws,
            skills,
            include_subagent=False,
            sandbox_policy=sandbox_policy,
            sandbox_backend=sandbox_backend,
            sandbox_roots=roots,
        )

    ai_cfg = getattr(app_cfg, "ai", None)
    return SubAgentRunner(
        app_cfg,
        engine_factory=lambda: build_engine_from_config(app_cfg),
        build_tools=build_tools,
        skills=skills,
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
        # P0-2: the lane a settled background run uses to reach the model.
        notices=notices,
        # M1b: the table a ``subAgent`` target is resolved against.
        agents=agents,
    )


def build_tool_registry(
    app_cfg: Any,
    workspace: str,
    skills: SkillRegistry | None = None,
    *,
    include_subagent: bool = True,
    user_prompt: Any | None = None,
    base_instructions: str = "",
    notices: Any | None = None,
    jobs: Any = None,
    agents: Any = None,
    sandbox_policy: Any = None,
    sandbox_backend: Any = None,
    sandbox_roots: Any = (),
    gate: Any = None,
    sandbox: Any = None,
) -> Any:
    """Build the *domain* tool registry used by the self-owned loop (R1a-4).

    The registry holds tools only: the permission gate and the approval callback
    belong to the ``ToolDispatcher``, which the loop builds.

    R6c: ``user_prompt`` is the channel ``ask_user`` would use.  A sub-agent stack
    (``include_subagent=False``) never gets it: delegating to an isolated worktree
    means there is nobody to ask, so the tool list must not pretend otherwise.

    M-D1: the process sandbox's policy/mechanism/roots arrive here so that
    ``BashTool`` shares the composition root's backend instance with
    ``JobRegistry``.  ``None``/empty leaves the behaviour exactly as before.

    E.2: ``gate``/``sandbox`` may be passed in by the composition root (world.py)
    so the job table and the tool stack share one gate and one live-roots source;
    when they are not, this builds its own, exactly as before.
    """
    registry, _gate = _assemble_registry(
        app_cfg,
        workspace,
        skills,
        include_subagent,
        user_prompt,
        base_instructions,
        notices,
        jobs,
        agents=agents,
        sandbox_policy=sandbox_policy,
        sandbox_backend=sandbox_backend,
        sandbox_roots=sandbox_roots,
        gate=gate,
        sandbox=sandbox,
    )
    return registry


def _assemble_registry(
    app_cfg: Any,
    workspace: str,
    skills: SkillRegistry | None,
    include_subagent: bool,
    user_prompt: Any | None = None,
    base_instructions: str = "",
    notices: Any | None = None,
    jobs: Any = None,
    agents: Any = None,
    sandbox_policy: Any = None,
    sandbox_backend: Any = None,
    sandbox_roots: Any = (),
    gate: Any = None,
    sandbox: Any = None,
) -> tuple[Any, Any]:
    """Create the sandbox/gate/registry triple shared by both builders."""

    from ai_coding.domain.agents import PLAN_DIR
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.bash_tool import BashTool
    from ai_coding.tools.edit_tool import EditTool
    from ai_coding.tools.glob_tool import GlobTool
    from ai_coding.tools.grep_tool import GrepTool
    from ai_coding.tools.read_tool import ReadTool
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.skill_tool import SkillTool
    from ai_coding.tools.todo_write_tool import TodoWriteTool
    from ai_coding.tools.webfetch_tool import WebfetchTool
    from ai_coding.tools.websearch_tool import WebsearchTool
    from ai_coding.tools.write_tool import WriteTool

    # B: the gate is built first and both halves of the boundary share it.  The
    # gate asks ("may this path, right now?") and the sandbox enforces, so an
    # approval the operator gave has to reach the tool -- a sandbox holding its
    # own copy of the answer would make "always" a promise nothing keeps.
    tools_cfg = app_cfg.tools
    if gate is None:
        gate = PermissionGate.from_config(app_cfg, workspace=workspace)
    if sandbox is None:
        if sandbox_roots:
            # A sub-agent stack (``build_subagent_runner`` passes the child's
            # worktree): its writable face is that worktree plus its own grants,
            # **never** the parent's configured extra roots (builder.py:351-353
            # says they are deliberately not passed down).
            sandbox = Sandbox(workspace, extra_roots=(), grantor=gate)
        else:
            sandbox = Sandbox(
                workspace,
                extra_roots=expand_root_patterns(
                    getattr(tools_cfg, "extra_workspace_roots", ()),
                    base=Path(workspace).expanduser().resolve(),
                ),
                grantor=gate,
            )
    fst = FileStateTracker()
    plan_root = sandbox.workspace / PLAN_DIR
    registry = ToolRegistry()

    if tools_cfg.bash.enabled:
        # E.1/E.2: the process layer's writable face is the path layer's, read
        # **live** -- the same roots, so a directory the file tools may write is
        # a directory a bash command may write, and never one or the other.
        registry.register(
            BashTool(
                timeout_seconds=tools_cfg.bash.timeout_seconds,
                policy=sandbox_policy,
                backend=sandbox_backend,
                roots=sandbox.live_roots,
            )
        )
    if tools_cfg.read.enabled:
        registry.register(
            ReadTool(sandbox, fst, max_file_size=tools_cfg.read.max_file_size)
        )
    if tools_cfg.write.enabled:
        registry.register(WriteTool(sandbox, fst, plan_root=plan_root))
    if tools_cfg.edit.enabled:
        registry.register(EditTool(sandbox, fst, plan_root=plan_root))
    if tools_cfg.glob.enabled:
        registry.register(GlobTool(sandbox))
    if getattr(getattr(tools_cfg, "grep", None), "enabled", True):
        registry.register(GrepTool(sandbox))

    # r17/T3: the two network tools.  Read-only toward the workspace; the gate
    # allows them (they only read the outside world), the SSRF guard bounds
    # *which* outside world webfetch will touch.
    ai_cfg = getattr(app_cfg, "ai", None)
    registry.register(WebfetchTool())
    registry.register(
        WebsearchTool(
            str(getattr(ai_cfg, "websearch_api_key", "") or ""),
            max_results=int(getattr(ai_cfg, "websearch_max_results", 5) or 5),
        )
    )

    # Always-registered tools
    # R9a: the dependency gate is on by default; legacy lists can switch it off.
    # ``app_cfg.ai`` is duck-typed (tests pass namespaces), so read it defensively.
    registry.register(
        TodoWriteTool(
            enforce_dependencies=bool(getattr(app_cfg.ai, "enforce_dependencies", True))
        )
    )

    if skills is not None:
        registry.register(SkillTool(skills))

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

    # 19 §3.2: the plan review needs a human — same gate as ask_user minus the
    # config switch: leaving plan mode is a core interaction, not a setting.
    if include_subagent and user_prompt is not None:
        from ai_coding.tools.present_plan_tool import PresentPlanTool

        registry.register(PresentPlanTool(user_prompt))

    # N1: the research exit.  It needs no human (it asks nobody), so it is
    # registered wherever a plan posture can exist: the main session.  A
    # sub-agent runs in a throwaway session and never plans.
    #
    # The *visibility* is the tool's own business: ``plan_only`` keeps it out
    # of the tool table outside the planning posture (the registry is
    # process-wide, so the posture cannot decide the registration -- see
    # ``BaseTool.plan_only`` and the loop's posture-aware tool surface).
    if include_subagent:
        from ai_coding.tools.finish_research_tool import FinishResearchTool

        registry.register(FinishResearchTool())

    if include_subagent:
        # D-4: the model's own taskification (start_goal / update_goal).  Main
        # session only, like present_plan: the goal run is dispatched by the
        # server on the MAIN session, and a sub-agent arming its throwaway
        # session's task would drive nothing.
        from ai_coding.tools.goal_tools import StartGoalTool, UpdateGoalTool

        registry.register(StartGoalTool())
        registry.register(UpdateGoalTool())

    if include_subagent:
        # C-1: read back what compaction dropped. Main-session only -- a
        # sub-agent runs on a throwaway SessionData, so its transcript is the
        # three messages it just produced (see test_builder).
        from ai_coding.tools.history_search_tool import HistorySearchTool

        registry.register(HistorySearchTool())

    if include_subagent and jobs is not None:
        # D-1: a job outlives the session that started it, so it belongs to the
        # workspace and to whoever holds the registry -- which is the main
        # session.  A sub-agent stack gets neither, exactly like history_search.
        from ai_coding.tools.job_tools import JobKillTool, JobListTool, JobOutputTool

        registry.register(JobListTool())
        registry.register(JobOutputTool())
        registry.register(JobKillTool())

    if include_subagent:
        # Real sub-agent delegation, isolated in a git worktree when enabled.
        # M-D1: the child's bash shares the parent's process sandbox.  The
        # roots are deliberately NOT passed down: the runner binds them to the
        # child's worktree, so a delegated write cannot land in the parent's
        # tree (or outside the child's worktree).
        runner = build_subagent_runner(
            app_cfg,
            workspace,
            skills,
            base_instructions=base_instructions,
            notices=notices,
            agents=agents,
            sandbox_policy=sandbox_policy,
            sandbox_backend=sandbox_backend,
        )
        manager = WorktreeManager(workspace)
        isolate = bool(getattr(app_cfg.ai, "subagent_worktree_isolation", True))
        registry.register(
            SubAgentTool(runner=runner, worktree_manager=manager, isolate=isolate)
        )
        # C-3b: the model's own view of the table /tasks reads.
        registry.register(SubAgentListTool(runner=runner))

    return registry, gate


__all__ = ["build_tool_registry", "build_subagent_runner"]
