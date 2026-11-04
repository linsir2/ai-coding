"""Read coverage records provenance; it must never trap a legitimate re-read."""

from __future__ import annotations

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import ToolContext
from ai_coding.tools.read_tool import ReadTool


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


def _tool(tmp_path) -> ReadTool:
    return ReadTool(Sandbox(tmp_path), FileStateTracker())


def _note(session: SessionData):
    def note(path, start, end, total, version, *, call_id="", canonical=False):
        session.note_read(
            path,
            start,
            end,
            total,
            version=version,
            call_id=call_id,
            canonical=canonical,
            workspace="",
        )

    return note


def _ctx(tmp_path, session: SessionData, *, messages=(), call_id="c1") -> ToolContext:
    return ToolContext(
        workspace=str(tmp_path),
        messages=tuple(messages),
        effects=lambda: session.effects,
        note_read=_note(session),
        call_id=call_id,
    )


def _visible(call_id: str) -> ChatMessage:
    return ChatMessage(
        role="tool",
        content="<tool_output tool=\"read\">already visible</tool_output>",
        timestamp="t",
        tool_call_id=call_id,
    )


@pytest.mark.asyncio
async def test_a_current_overlapping_read_still_returns_the_page(tmp_path):
    """The ledger is provenance, not a veto.  A model that needs the exact
    bytes again (after a failed edit) must get them instead of a refusal."""
    path = tmp_path / "a.py"
    path.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    session = _session()
    tool = _tool(tmp_path)

    first = await tool.execute(
        {"path": "a.py", "offset": 0, "limit": 50},
        _ctx(tmp_path, session, call_id="c1"),
    )
    assert first.success is True

    second = await tool.execute(
        {"path": "a.py", "offset": 10, "limit": 20},
        _ctx(tmp_path, session, messages=(_visible("c1"),), call_id="c2"),
    )

    assert second.success is True
    assert "line 11" in second.output


@pytest.mark.asyncio
async def test_a_read_is_allowed_after_the_old_result_left_the_context(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    session = _session()
    tool = _tool(tmp_path)

    await tool.execute(
        {"path": "a.py", "offset": 0, "limit": 50},
        _ctx(tmp_path, session, call_id="c1"),
    )
    second = await tool.execute(
        {"path": "a.py", "offset": 10, "limit": 20},
        _ctx(tmp_path, session, messages=(), call_id="c2"),
    )

    assert second.success is True


@pytest.mark.asyncio
async def test_a_read_is_allowed_after_the_file_version_changes(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    session = _session()
    tool = _tool(tmp_path)

    await tool.execute(
        {"path": "a.py", "offset": 0, "limit": 50},
        _ctx(tmp_path, session, call_id="c1"),
    )
    path.write_text(
        "\n".join(f"line {i}" for i in range(1, 101)) + "\nextra",
        encoding="utf-8",
    )

    second = await tool.execute(
        {"path": "a.py", "offset": 10, "limit": 20},
        _ctx(tmp_path, session, messages=(_visible("c1"),), call_id="c2"),
    )

    assert second.success is True


@pytest.mark.asyncio
async def test_a_legacy_mark_without_call_id_does_not_refuse(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    session = _session()
    session.note_read("a.py", 1, 50, 100, version="v1", workspace=str(tmp_path))
    tool = _tool(tmp_path)

    result = await tool.execute(
        {"path": "a.py", "offset": 10, "limit": 20},
        _ctx(tmp_path, session, call_id="c2"),
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_symlink_spellings_share_one_effect_key(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    (tmp_path / "link.py").symlink_to(real)
    session = _session()
    tool = _tool(tmp_path)

    first = await tool.execute(
        {"path": "link.py", "offset": 0, "limit": 50},
        _ctx(tmp_path, session, call_id="c1"),
    )
    assert first.success is True

    second = await tool.execute(
        {"path": "real.py", "offset": 10, "limit": 20},
        _ctx(tmp_path, session, messages=(_visible("c1"),), call_id="c2"),
    )

    assert second.success is True
    assert "line 11" in second.output


def _user(content: str = "next turn") -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp="t")


@pytest.mark.asyncio
async def test_a_new_human_turn_may_re_read_a_visible_range(tmp_path):
    r"""The previous turn's result is still in history, but the model may need
    the exact bytes again for an edit.  Returning ``already_read`` forever made
    the model retry the same read and then edit from a stale copy."""
    path = tmp_path / "a.py"
    path.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    session = _session()
    tool = _tool(tmp_path)

    await tool.execute(
        {"path": "a.py", "offset": 0, "limit": 100},
        _ctx(tmp_path, session, call_id="c1"),
    )
    second = await tool.execute(
        {"path": "a.py", "offset": 0, "limit": 100},
        _ctx(
            tmp_path,
            session,
            messages=(_visible("c1"), _user("continue")),
            call_id="c2",
        ),
    )

    assert second.success is True
    assert "line 1" in second.output
