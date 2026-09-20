"""Sub-agent orchestration (M4-1).

A sub-agent is a self-contained run with its own micro-tool-stack.  By default it
is isolated in a fresh git worktree (M4-2) when the workspace is a repo and
``isolate=True``; otherwise it runs against the main workspace.  Only the final
conclusion is returned — intermediate tool activity is discarded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ai_coding.ai.factory import build_service_from_config
from ai_coding.core.worktree import WorktreeError, WorktreeManager
from ai_coding.domain.subagent import SubagentResult

# ``tools`` is passed by keyword; a fully-annotated Callable would need a
# named parameter, so use ``...`` to stay permissive across factories.
BuildService = Callable[..., Any]
BuildTools = Callable[[str | None], list[Any]]


class SubAgentRunner:
    """Run one sub-agent task, optionally inside a git worktree sandbox."""

    def __init__(
        self,
        app_config: Any,
        build_service: BuildService | None = None,
        build_tools: BuildTools | None = None,
    ) -> None:
        self._app_config = app_config
        self._build_service: BuildService = build_service or build_service_from_config
        self._build_tools: BuildTools | None = build_tools

    async def run(
        self,
        task: str,
        name: str = "sub-agent",
        *,
        worktree_manager: WorktreeManager | None = None,
        isolate: bool = True,
    ) -> SubagentResult:
        """Run the task and return its conclusion.

        When isolation is enabled and a ``worktree_manager`` reports a git repo,
        the sub-agent works and edits inside its own worktree; any worktree
        failure degrades to the main workspace rather than failing the task.
        """
        if isolate and worktree_manager is not None and await worktree_manager.can_isolate():
            try:
                async with worktree_manager.temporary(name) as wt:
                    return await self._run_in(task, name, str(wt.path))
            except WorktreeError:
                pass  # fallthrough: run against the main workspace
        return await self._run_in(task, name, None)

    async def _run_in(self, task: str, name: str, workdir: str | None) -> SubagentResult:
        tools = self._build_tools(workdir) if self._build_tools is not None else None
        service = self._build_service(self._app_config, tools=tools)
        prompt = (
            f"You are {name}, a sub-agent. Complete the following task and return "
            f"your final answer:\n\n{task}"
        )
        turn = await service.execute_turn([], prompt)
        return SubagentResult(text=turn.text or None)


__all__ = ["SubAgentRunner"]
