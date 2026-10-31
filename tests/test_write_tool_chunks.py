"""TDD: the write tool must make a large file a sequence of small calls.

Trace 2026-10-31 (f1d881ee, turn 12) measured the failure: the model put a
30k-char document in one ``write`` call, the provider cut the arguments, the
JSON was invalid, and the model repeated the same giant call (four
``invalid_arguments`` in one turn).  A per-call cap plus an append mode turns
that one impossible call into several parseable ones.
"""

from pathlib import Path

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import ToolContext
from ai_coding.tools.write_tool import MAX_CHUNK_CHARS, WriteTool


def _tool(tmp_path: Path) -> WriteTool:
    return WriteTool(Sandbox(str(tmp_path)), FileStateTracker())


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=str(tmp_path))


@pytest.mark.asyncio
async def test_append_extends_the_file_in_several_calls(tmp_path):
    tool = _tool(tmp_path)
    first = await tool.execute(
        {"path": "doc.md", "content": "part one\n"}, _ctx(tmp_path)
    )
    second = await tool.execute(
        {"path": "doc.md", "content": "part two\n", "mode": "append"},
        _ctx(tmp_path),
    )

    assert first.success is True
    assert second.success is True
    assert second.output.startswith("appended ")
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "part one\npart two\n"


@pytest.mark.asyncio
async def test_one_call_over_the_chunk_cap_is_refused_before_writing(tmp_path):
    tool = _tool(tmp_path)
    oversized = "x" * (MAX_CHUNK_CHARS + 1)

    result = await tool.execute(
        {"path": "doc.md", "content": oversized}, _ctx(tmp_path)
    )

    assert result.success is False
    assert result.error == "content_too_large"
    assert "mode='append'" in result.output
    assert not (tmp_path / "doc.md").exists()


@pytest.mark.asyncio
async def test_append_to_an_unread_existing_file_is_refused(tmp_path):
    (tmp_path / "doc.md").write_text("existing", encoding="utf-8")
    tool = _tool(tmp_path)

    result = await tool.execute(
        {"path": "doc.md", "content": "more", "mode": "append"}, _ctx(tmp_path)
    )

    assert result.success is False
    assert result.error == "must_read_first"


@pytest.mark.asyncio
async def test_an_empty_append_still_succeeds(tmp_path):
    """A model that has written the whole file may issue a final empty append."""
    tool = _tool(tmp_path)
    await tool.execute({"path": "doc.md", "content": "body"}, _ctx(tmp_path))
    result = await tool.execute(
        {"path": "doc.md", "content": "", "mode": "append"}, _ctx(tmp_path)
    )

    assert result.success is True
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "body"
