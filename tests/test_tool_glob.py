"""TDD (M2-1): glob tool."""

import time

import pytest

from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.glob_tool import GlobTool


@pytest.fixture
def setup(tmp_path):
    sb = Sandbox(tmp_path)
    tool = GlobTool(sb)
    return tmp_path, sb, tool


@pytest.mark.asyncio
async def test_glob_finds_files(setup):
    tmp_path, sb, tool = setup
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    result = await tool.execute({"pattern": "*.py"})
    assert result.success is True
    assert "a.py" in result.output
    assert "b.py" in result.output
    assert "c.txt" not in result.output


@pytest.mark.asyncio
async def test_glob_recursive(setup):
    tmp_path, sb, tool = setup
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("")
    result = await tool.execute({"pattern": "**/*.py"})
    assert result.success is True
    assert "x.py" in result.output


@pytest.mark.asyncio
async def test_glob_max_results(setup):
    tmp_path, sb, tool = setup
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("")
    result = await tool.execute({"pattern": "*.txt", "max_results": 3})
    assert result.success is True
    # only 3 results shown
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) <= 3 + 1  # +1 for truncation notice


@pytest.mark.asyncio
async def test_glob_sorted_by_mtime(setup):
    tmp_path, sb, tool = setup
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("")
    time.sleep(0.01)
    b.write_text("")  # newer
    result = await tool.execute({"pattern": "*.txt"})
    assert result.success is True
    lines = [line.strip() for line in result.output.strip().split("\n") if line.strip()]
    # b should come first (newer)
    assert lines[0].endswith("b.txt") or "b.txt" in lines[0]


@pytest.mark.asyncio
async def test_glob_outside_sandbox_denied(setup):
    tmp_path, sb, tool = setup
    result = await tool.execute({"pattern": "../*"})
    assert result.success is False
