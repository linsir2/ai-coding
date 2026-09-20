"""TDD (M4-1): SubAgentRunner — orchestrate one sub-agent execution.

The runner bolts a worktree-managed sandbox (isolate) / main workspace (plain)
onto the AI service and returns only the sub-agent's conclusion.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ai_coding.core.subagent import SubAgentRunner
from ai_coding.core.worktree import WorktreeError
from ai_coding.domain.subagent import SubagentResult


class _FakeService:
    def __init__(self) -> None:
        self.prompt: str | None = None
        self.tools: object | None = None
        self.reply: str = "sub-agent conclusion"

    async def execute_turn(self, history, user_input, token_sink=None, *, instructions=None):
        self.prompt = user_input
        from ai_coding.domain.run import TurnResult

        return TurnResult(text=self.reply)


class _FakeWT:
    """Async-context-manager worktree fake; ``temporary`` is an asynccontextmanager."""

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


def _make_runner(svc: _FakeService, seen: list) -> SubAgentRunner:
    def build_service(cfg, tools=None):
        svc.tools = tools
        return svc

    def build_tools(workdir):
        seen.append(workdir)
        return ["TOOLS"]

    return SubAgentRunner(object(), build_service=build_service, build_tools=build_tools)


@pytest.mark.asyncio
async def test_run_plain_builds_tools_for_main_and_returns_text():
    svc = _FakeService()
    seen: list = []
    runner = _make_runner(svc, seen)

    res = await runner.run("analyze the bug", "analyst", isolate=False)

    assert isinstance(res, SubagentResult)
    assert res.text == "sub-agent conclusion"
    assert svc.tools == ["TOOLS"]
    assert seen == [None]  # main workspace (workdir None) tool stack was built
    assert svc.prompt is not None and "analyze the bug" in svc.prompt


@pytest.mark.asyncio
async def test_run_isolated_uses_worktree_path():
    svc = _FakeService()
    seen: list = []
    runner = _make_runner(svc, seen)

    res = await runner.run("task", "dev", worktree_manager=_FakeWT(), isolate=True)

    assert res.text == "sub-agent conclusion"
    assert seen == ["wt/path"]  # tools were bound to the worktree


@pytest.mark.asyncio
async def test_run_isolated_falls_back_to_main_on_worktree_error():
    svc = _FakeService()
    seen: list = []
    runner = _make_runner(svc, seen)

    res = await runner.run("task", "dev", worktree_manager=_FakeWT(fail=True), isolate=True)

    assert res.text == "sub-agent conclusion"
    assert seen == [None]  # degraded to the main workspace


@pytest.mark.asyncio
async def test_run_defaults_to_sub_agent_name_and_no_isolation_ok():
    svc = _FakeService()
    seen: list = []
    runner = _make_runner(svc, seen)

    res = await runner.run("just do it")

    assert res.text == "sub-agent conclusion"
    assert svc.prompt is not None and "sub-agent" in svc.prompt
