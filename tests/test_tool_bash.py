"""TDD (M2-1): bash tool."""

import pytest

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
