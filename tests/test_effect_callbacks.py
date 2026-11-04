"""Successful write/edit tools must report the effect immediately."""

from __future__ import annotations

import pytest

from ai_coding.domain.session import SessionData
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import ToolContext
from ai_coding.tools.edit_tool import EditTool
from ai_coding.tools.write_tool import WriteTool


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


def _ctx(*, note_effect=None, call_id="c1") -> ToolContext:
    return ToolContext(note_effect=note_effect, call_id=call_id)


@pytest.mark.asyncio
async def test_write_reports_the_effect_immediately(tmp_path):
    seen = []
    tool = WriteTool(Sandbox(tmp_path), FileStateTracker())

    result = await tool.execute(
        {"path": "a.py", "content": "x\n"},
        _ctx(note_effect=lambda *row, **kw: seen.append((row, kw))),
    )

    assert result.success is True
    assert seen
    row, kw = seen[0]
    assert row[0] == "a.py"
    assert row[2:] == ("write", "write")
    assert kw["call_id"] == "c1"
    assert row[1]


@pytest.mark.asyncio
async def test_edit_reports_the_effect_immediately(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("one\n", encoding="utf-8")
    tracker = FileStateTracker()
    tracker.record(target)
    seen = []
    tool = EditTool(Sandbox(tmp_path), tracker)

    result = await tool.execute(
        {"path": "a.py", "old_string": "one", "new_string": "two"},
        _ctx(note_effect=lambda *row, **kw: seen.append((row, kw))),
    )

    assert result.success is True
    assert seen
    row, kw = seen[0]
    assert row[0] == "a.py"
    assert row[2:] == ("edit", "write")
    assert kw["call_id"] == "c1"


def test_session_record_effect_invalidates_the_old_read():
    session = _session()
    session.note_read("a.py", 1, 10, 10, version="v1", call_id="c1")

    session.record_effect("a.py", "v2", tool="write", call_id="c2", kind="write")

    assert session.effects.covering_read("a.py", 1, 10, "v1") is None
    assert session.effects.receipt_for("a.py") is not None
