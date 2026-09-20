"""TDD (M2-1): write tool (sandbox + stale-read)."""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.write_tool import WriteTool


@pytest.fixture
def setup(tmp_path):
    sb = Sandbox(tmp_path)
    fst = FileStateTracker()
    tool = WriteTool(sb, fst)
    return tmp_path, sb, fst, tool


@pytest.mark.asyncio
async def test_write_creates_file(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "new.txt", "content": "hello"})
    assert result.success is True
    assert (tmp_path / "new.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_write_overwrites(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("old")
    fst.record(tmp_path / "f.txt")  # simulate prior read
    result = await tool.execute({"path": "f.txt", "content": "new"})
    assert result.success is True
    assert (tmp_path / "f.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_write_creates_parent_dirs(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "a/b/c.txt", "content": "x"})
    assert result.success is True
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "x"


@pytest.mark.asyncio
async def test_write_outside_sandbox_denied(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "/tmp/evil.txt", "content": "x"})
    assert result.success is False
    assert "workspace" in result.output.lower() or "sandbox" in result.output.lower()


@pytest.mark.asyncio
async def test_write_without_prior_read_denied(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("existing")
    # never read → stale check fails
    result = await tool.execute({"path": "f.txt", "content": "new"})
    assert result.success is False
    out = result.output.lower()
    assert "read" in out or "stale" in out or "never" in out


@pytest.mark.asyncio
async def test_write_stale_after_external_modification(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    p.write_text("v1")
    fst.record(p)  # snapshot of v1
    p.write_text("v2 externally changed")  # external modification
    result = await tool.execute({"path": "f.txt", "content": "overwrite"})
    assert result.success is False
    assert "stale" in result.output.lower() or "modified" in result.output.lower()
