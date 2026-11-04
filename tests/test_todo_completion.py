"""todo_write completion is receipt-gated, with one approved override per session."""

from __future__ import annotations

import pytest

from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.infra.file_utils import file_version
from ai_coding.security.approval import ApprovalResult, AutoApproveCallback
from ai_coding.tools.base import ToolContext
from ai_coding.tools.todo_write_tool import TodoWriteTool


class _Approval:
    is_human = True

    def __init__(self, answer=ApprovalResult.YES):
        self.answer = answer
        self.calls = 0

    async def approve(self, tool_name, params, reason):
        self.calls += 1
        return self.answer


def _session(files: list[str] | None = None) -> SessionData:
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = TaskState(goal="g")
    item = session.task.add("create a.py", files=files or [])
    item.started_seq = 1
    return session


def _ctx(session, tmp_path, *, approved=False, approval=None, note_override=None):
    return ToolContext(
        task=session.task,
        workspace=str(tmp_path),
        effects=lambda: session.effects,
        approval=approval,
        completion_override_approved=approved,
        note_completion_override=note_override,
    )


@pytest.mark.asyncio
async def test_completion_without_a_receipt_is_rejected(tmp_path):
    session = _session(["a.py"])
    (tmp_path / "a.py").write_text("x", encoding="utf-8")

    result = await TodoWriteTool().execute(
        {"action": "update", "id": "1", "status": "completed"},
        _ctx(session, tmp_path),
    )

    assert result.success is False
    assert result.error == "completion_unverified"
    assert session.task.items[0].status == "pending"
    assert session.task.items[0].completion_failures == 1


@pytest.mark.asyncio
async def test_a_current_receipt_allows_completion(tmp_path):
    session = _session(["a.py"])
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.record_effect("a.py", file_version(target), tool="write", call_id="c1")

    result = await TodoWriteTool().execute(
        {"action": "update", "id": "1", "status": "completed"},
        _ctx(session, tmp_path),
    )

    assert result.success is True
    assert session.task.items[0].status == "completed"


@pytest.mark.asyncio
async def test_the_first_override_asks_and_the_session_remembers_it(tmp_path):
    session = _session()
    approval = _Approval()
    remembered = {"ok": False}

    first = await TodoWriteTool().execute(
        {
            "action": "update",
            "id": "1",
            "status": "completed",
            "override": True,
            "override_reason": "non-file task",
        },
        _ctx(
            session,
            tmp_path,
            approval=approval,
            note_override=lambda: remembered.__setitem__("ok", True),
        ),
    )

    assert first.success is True
    assert approval.calls == 1
    assert remembered["ok"] is True
    assert session.task.items[0].status == "completed"

    # A second item uses the remembered session approval, so no second prompt.
    session.task.add("another non-file task")
    approval_again = _Approval()
    second = await TodoWriteTool().execute(
        {
            "action": "update",
            "id": "2",
            "status": "completed",
            "override": True,
            "override_reason": "same session",
        },
        _ctx(session, tmp_path, approved=True, approval=approval_again),
    )

    assert second.success is True
    assert approval_again.calls == 0


@pytest.mark.asyncio
async def test_a_denied_override_does_not_complete(tmp_path):
    session = _session()
    approval = _Approval(ApprovalResult.NO)

    result = await TodoWriteTool().execute(
        {
            "action": "update",
            "id": "1",
            "status": "completed",
            "override": True,
            "override_reason": "blocked",
        },
        _ctx(session, tmp_path, approval=approval),
    )

    assert result.success is False
    assert result.error == "override_denied"
    assert session.task.items[0].status == "pending"


@pytest.mark.asyncio
async def test_two_rejections_block_the_item(tmp_path):
    session = _session(["a.py"])
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    tool = TodoWriteTool()

    first = await tool.execute(
        {"action": "update", "id": "1", "status": "completed"},
        _ctx(session, tmp_path),
    )
    second = await tool.execute(
        {"action": "update", "id": "1", "status": "completed"},
        _ctx(session, tmp_path),
    )

    assert first.success is False
    assert second.success is False
    assert second.error == "completion_blocked"
    assert session.task.items[0].status == "blocked"


@pytest.mark.asyncio
async def test_completion_uses_files_supplied_by_the_same_call(tmp_path):
    session = _session()
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.record_effect("a.py", file_version(target), tool="write", call_id="c1")

    result = await TodoWriteTool().execute(
        {
            "action": "update",
            "id": "1",
            "status": "completed",
            "files": ["a.py"],
        },
        _ctx(session, tmp_path),
    )

    assert result.success is True
    assert session.task.items[0].status == "completed"


@pytest.mark.asyncio
async def test_completion_cannot_swap_in_a_file_without_its_own_receipt(tmp_path):
    session = _session()
    old = tmp_path / "old.py"
    old.write_text("x", encoding="utf-8")
    session.record_effect("old.py", file_version(old), tool="write", call_id="c1")
    session.task.items[0].files = ["old.py"]

    result = await TodoWriteTool().execute(
        {
            "action": "update",
            "id": "1",
            "status": "completed",
            "files": ["new.py"],
        },
        _ctx(session, tmp_path),
    )

    assert result.success is False
    assert session.task.items[0].status == "pending"


@pytest.mark.asyncio
async def test_auto_approval_cannot_grant_a_completion_override(tmp_path):
    session = _session()
    auto = AutoApproveCallback()

    result = await TodoWriteTool().execute(
        {
            "action": "update",
            "id": "1",
            "status": "completed",
            "override": True,
            "override_reason": "automated",
        },
        _ctx(session, tmp_path, approval=auto),
    )

    assert result.success is False
    assert result.error == "override_unavailable"
    assert session.task.items[0].status == "pending"
