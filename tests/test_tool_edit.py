"""TDD (M2-1): edit tool (string replacement + stale-read)."""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.edit_tool import EditTool


@pytest.fixture
def setup(tmp_path):
    sb = Sandbox(tmp_path)
    fst = FileStateTracker()
    tool = EditTool(sb, fst)
    return tmp_path, sb, fst, tool


def _write_and_record(path, text, fst):
    path.write_text(text)
    fst.record(path)


@pytest.mark.asyncio
async def test_edit_replace(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    _write_and_record(p, "hello world", fst)
    result = await tool.execute({"path": "f.txt", "old_string": "world", "new_string": "there"})
    assert result.success is True
    assert p.read_text() == "hello there"


@pytest.mark.asyncio
async def test_edit_old_string_not_found(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    _write_and_record(p, "hello world", fst)
    result = await tool.execute({"path": "f.txt", "old_string": "nope", "new_string": "x"})
    assert result.success is False
    assert "not found" in result.output.lower() or "match" in result.output.lower()


@pytest.mark.asyncio
async def test_edit_old_string_not_unique(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    _write_and_record(p, "foo foo foo", fst)
    result = await tool.execute({"path": "f.txt", "old_string": "foo", "new_string": "bar"})
    assert result.success is False
    assert "unique" in result.output.lower() or "multiple" in result.output.lower()


@pytest.mark.asyncio
async def test_edit_replace_all(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    _write_and_record(p, "foo foo foo", fst)
    result = await tool.execute({
        "path": "f.txt", "old_string": "foo", "new_string": "bar", "replace_all": True,
    })
    assert result.success is True
    assert p.read_text() == "bar bar bar"


@pytest.mark.asyncio
async def test_edit_requires_prior_read(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    p.write_text("hi")
    # never recorded → no prior read
    result = await tool.execute({"path": "f.txt", "old_string": "hi", "new_string": "bye"})
    assert result.success is False
    out = result.output.lower()
    assert "read" in out or "stale" in out or "never" in out


@pytest.mark.asyncio
async def test_edit_stale_denied(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    _write_and_record(p, "v1", fst)
    p.write_text("v2 externally changed")  # stale
    result = await tool.execute({"path": "f.txt", "old_string": "v2", "new_string": "v3"})
    assert result.success is False
    assert "stale" in result.output.lower() or "modified" in result.output.lower()
