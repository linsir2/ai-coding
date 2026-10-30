"""TDD (r25/v2): the model-facing handoff, the branch handoff, the notice.

The contract's shape is frozen in test_subagent_contract.py / structured_handoff;
this file pins the v2 content decisions: the parent reads only decision-relevant
lines, an isolated child's writes survive as a branch, and a background
settlement states a schema failure.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.gitcmd import git
from ai_coding.core.steering import SteeringQueue
from ai_coding.core.subagent import SubAgentRunner, _notice_text
from ai_coding.core.worktree import WorktreeManager
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import BackgroundTask, SubagentResult
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.subagent_tool import _render


def _result(**overrides) -> SubagentResult:
    base = SubagentResult(
        status="completed",
        conclusion="done",
        iterations=3,
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        workdir="/tmp/dead-worktree",
        ledger_path="/tmp/ledger.json",
    )
    return replace(base, **overrides)


def test_the_model_report_drops_diagnostics():
    text = _render(_result(), "probe")

    assert text.startswith("[probe] completed")
    assert "tokens:" not in text
    assert "工作区:" not in text
    # Completed + valid shape: the ledger is not needed to make a decision.
    assert "完整回包" not in text
    assert "done" in text


def test_the_ledger_pointer_appears_only_when_the_report_is_incomplete():
    cut = _render(_result(truncated=True, exit_reason="timeout"), "probe")
    assert "完整回包" in cut

    invalid = _render(
        _result(schema_valid=False, schema_errors=("must be string",)), "probe"
    )
    assert "完整回包" in invalid


def test_the_branch_and_commit_are_reported():
    text = _render(
        _result(worktree_branch="ai_sub_fix_abc", commit="1234567890"), "probe"
    )
    assert "ai_sub_fix_abc" in text
    assert "未合并" in text
    assert "1234567" in text


def test_the_background_notice_names_the_branch():
    line = _notice_text(
        "run-1", "fixer", "completed", "did it", "/tmp/ledger.json",
        "ai_sub_fix_abc",
    )
    assert "分支 ai_sub_fix_abc（未合并）" in line
    assert "完整回包 /tmp/ledger.json" in line


def test_the_background_notice_marks_a_schema_failure():
    notices = SteeringQueue()
    runner = SubAgentRunner(
        object(),
        engine_factory=lambda: None,  # type: ignore[arg-type]
        build_tools=lambda workdir: ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        notices=notices,
        workspace=".",
    )
    runner._background["run-1"] = BackgroundTask(  # noqa: SLF001
        run_id="run-1", name="probe", status="running"
    )
    runner._announce["run-1"] = (lambda event: None, "origin", "probe")  # noqa: SLF001

    runner._finish_background(  # noqa: SLF001
        "run-1",
        SubagentResult(status="completed", conclusion="ok", schema_valid=False),
        lambda event: None,
        "origin",
    )

    line = notices.drain()[0]
    assert "结构校验未过" in line


class _CommittingManager:
    """A worktree manager that reports one commit (the real one is in test_worktree)."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def can_isolate(self) -> bool:
        return True

    def temporary(self, name: str):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _cm(_name: str):
            yield SimpleNamespace(path="/tmp/wt", branch="ai_sub_probe_1")

        return _cm(name)

    async def commit_if_dirty(self, path: str, message: str) -> str | None:
        self.messages.append(message)
        return "abcdef123456"


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="done", tool_calls=[], finish_reason="stop")


@pytest.mark.asyncio
async def test_an_isolated_child_hands_back_its_branch(tmp_path):
    manager = _CommittingManager()
    runner = SubAgentRunner(
        object(),
        engine_factory=lambda: _Engine(),
        build_tools=lambda workdir: ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=".",
        ledger_root=tmp_path,
    )

    result = await runner.run("edit it", "probe", worktree_manager=manager)

    assert result.worktree_branch == "ai_sub_probe_1"
    assert result.commit == "abcdef123456"
    assert manager.messages == ["subagent: probe"]
    # The ledger is written before the commit, so it must be refreshed: the
    # "full packet" file has to name the branch too.
    assert result.ledger_path
    payload = json.loads(Path(result.ledger_path).read_text(encoding="utf-8"))
    assert payload["v"] == 2
    assert payload["worktree_branch"] == "ai_sub_probe_1"
    assert payload["commit"] == "abcdef123456"


@pytest.mark.asyncio
async def test_commit_if_dirty_keeps_the_change_on_the_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert (await git(repo, "init"))[0] == 0
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    assert (await git(repo, "add", "-A"))[0] == 0
    assert (
        await git(
            repo, "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-m", "base",
        )
    )[0] == 0

    manager = WorktreeManager(repo)
    worktree = await manager.create("probe")
    (worktree.path / "a.txt").write_text("changed\n", encoding="utf-8")
    (worktree.path / "new.txt").write_text("new\n", encoding="utf-8")

    sha = await manager.commit_if_dirty(str(worktree.path), "subagent: probe")
    assert sha and len(sha) == 40

    await manager.remove(str(worktree.path))

    code, text = await git(
        repo, "show", f"{worktree.branch}:a.txt", merge_stderr=False
    )
    assert code == 0 and text.strip() == "changed"
    code, text = await git(
        repo, "show", f"{worktree.branch}:new.txt", merge_stderr=False
    )
    assert code == 0 and text.strip() == "new"


@pytest.mark.asyncio
async def test_commit_if_dirty_is_none_when_nothing_changed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert (await git(repo, "init"))[0] == 0
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    assert (await git(repo, "add", "-A"))[0] == 0
    assert (
        await git(
            repo, "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-m", "base",
        )
    )[0] == 0

    manager = WorktreeManager(repo)
    worktree = await manager.create("probe")
    try:
        assert await manager.commit_if_dirty(str(worktree.path), "subagent: probe") is None
    finally:
        await manager.remove(str(worktree.path))
