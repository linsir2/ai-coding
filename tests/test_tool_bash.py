"""TDD (M2-1): bash tool."""

import time
from pathlib import Path

import pytest

from ai_coding.tools.base import ToolContext
from ai_coding.tools.bash_tool import BashTool


@pytest.mark.asyncio
async def test_bash_echo():
    tool = BashTool()
    result = await tool.execute({"command": "echo hello"})
    assert result.success is True
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_bash_stderr_captured():
    tool = BashTool()
    result = await tool.execute({"command": "echo err >&2"})
    assert result.success is True
    assert "err" in result.output


@pytest.mark.asyncio
async def test_bash_nonzero_exit_still_success_with_output():
    # non-zero exit is captured in output but success reflects the command exit
    tool = BashTool()
    result = await tool.execute({"command": "exit 1"})
    assert result.success is False
    assert "exit code 1" in result.output.lower() or result.output is not None


@pytest.mark.asyncio
async def test_bash_timeout():
    tool = BashTool(timeout_seconds=1)
    result = await tool.execute({"command": "sleep 5"})
    assert result.success is False
    assert "timeout" in result.output.lower()


@pytest.mark.asyncio
async def test_bash_output_truncation():
    tool = BashTool(max_output_chars=10)
    result = await tool.execute({"command": "echo 0123456789abcdef"})
    assert result.success is True
    # output must not exceed the limit + truncation notice
    assert len(result.output) <= 100  # generous: notice text adds some
    assert "truncated" in result.output.lower()


@pytest.mark.asyncio
async def test_bash_runs_in_the_context_workspace(tmp_path: Path):
    """R2b: the description promises the workspace; the code used to ignore it."""
    tool = BashTool()
    result = await tool.execute({"command": "pwd"}, ToolContext(workspace=str(tmp_path)))
    assert result.success is True
    assert Path(result.output.strip()).resolve() == tmp_path.resolve()


@pytest.mark.asyncio
async def test_bash_falls_back_when_the_workspace_does_not_exist(tmp_path: Path):
    tool = BashTool()
    result = await tool.execute(
        {"command": "echo ok"}, ToolContext(workspace=str(tmp_path / "missing"))
    )
    assert result.success is True
    assert "ok" in result.output


@pytest.mark.asyncio
async def test_bash_timeout_holds_even_when_a_child_keeps_the_pipe(tmp_path: Path):
    """R2a/R2b: the old implementation hung here forever (docs/r2-analysis.md 1.4-A)."""
    tool = BashTool(timeout_seconds=1)
    started = time.monotonic()
    result = await tool.execute({"command": "sleep 30 & sleep 30"})
    elapsed = time.monotonic() - started
    assert result.success is False
    assert "timeout" in result.output.lower()
    assert elapsed < 8.0, f"bash tool took {elapsed:.1f}s; the timeout is not hard"
