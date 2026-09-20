"""TDD (M2-1): read tool (with sandbox + file state tracker)."""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.read_tool import ReadTool


@pytest.fixture
def setup(tmp_path):
    sb = Sandbox(tmp_path)
    fst = FileStateTracker()
    tool = ReadTool(sb, fst)
    return tmp_path, sb, fst, tool


@pytest.mark.asyncio
async def test_read_basic(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("hello\nworld\n")
    result = await tool.execute({"path": "f.txt"})
    assert result.success is True
    assert "hello" in result.output
    assert "world" in result.output


@pytest.mark.asyncio
async def test_read_line_numbers(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("a\nb\nc\n")
    result = await tool.execute({"path": "f.txt"})
    assert "     1  a" in result.output or "1 | a" in result.output or "1:" in result.output


@pytest.mark.asyncio
async def test_read_offset_limit(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    result = await tool.execute({"path": "f.txt", "offset": 3, "limit": 2})
    assert result.success is True
    assert "line2" not in result.output  # 0-indexed offset 3 = line 4
    assert "line3" in result.output
    assert "line4" in result.output
    assert "line5" not in result.output


@pytest.mark.asyncio
async def test_read_nonexistent_fails(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "nope.txt"})
    assert result.success is False
    assert "not found" in result.output.lower() or "no such" in result.output.lower()


@pytest.mark.asyncio
async def test_read_records_snapshot(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("hi")
    await tool.execute({"path": "f.txt"})
    assert fst.check(tmp_path / "f.txt").name == "CLEAN"


@pytest.mark.asyncio
async def test_read_outside_sandbox_denied(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "/etc/passwd"})
    assert result.success is False
    assert "workspace" in result.output.lower() or "sandbox" in result.output.lower()
