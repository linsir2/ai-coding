"""Completion requires a receipt or verify; self-reported evidence is not enough."""

from __future__ import annotations

import pytest

from ai_coding.core.completion import CompletionGate
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.infra.file_utils import file_version
from ai_coding.tools.base import ToolContext


def _task(files: list[str]) -> TaskState:
    task = TaskState(goal="g")
    task.add("create a.py", files=files)
    return task


def _ctx(tmp_path, session: SessionData, *, verify=None) -> ToolContext:
    return ToolContext(
        task=session.task,
        workspace=str(tmp_path),
        effects=lambda: session.effects,
        verify_item=verify,
    )


@pytest.mark.asyncio
async def test_a_pending_item_without_receipt_or_verify_is_rejected(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["a.py"])
    (tmp_path / "a.py").write_text("x", encoding="utf-8")

    verdict = await CompletionGate.check(session.task.items[0], _ctx(tmp_path, session))

    assert verdict.ok is False
    assert verdict.reason


@pytest.mark.asyncio
async def test_a_current_receipt_completes_the_item(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["a.py"])
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.record_effect("a.py", file_version(target), tool="write", call_id="c1")

    verdict = await CompletionGate.check(session.task.items[0], _ctx(tmp_path, session))

    assert verdict.ok is True
    assert "receipt" in verdict.evidence


@pytest.mark.asyncio
async def test_a_stale_receipt_is_rejected(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["a.py"])
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.record_effect("a.py", "old-version", tool="write", call_id="c1")

    verdict = await CompletionGate.check(session.task.items[0], _ctx(tmp_path, session))

    assert verdict.ok is False
    assert verdict.reason


@pytest.mark.asyncio
async def test_a_receipt_before_started_seq_is_rejected(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["a.py"])
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.record_effect("a.py", file_version(target), tool="write", call_id="c1")
    session.task.items[0].started_seq = 2

    verdict = await CompletionGate.check(session.task.items[0], _ctx(tmp_path, session))

    assert verdict.ok is False
    assert verdict.reason


@pytest.mark.asyncio
async def test_verify_is_an_alternative_to_a_file_receipt(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["a.py"])
    session.task.items[0].verify = "test -f a.py"

    async def verify(item_id, statement, command):
        return True, "ok"

    verdict = await CompletionGate.check(
        session.task.items[0], _ctx(tmp_path, session, verify=verify)
    )

    assert verdict.ok is True
    assert "verify" in verdict.evidence


@pytest.mark.asyncio
async def test_failed_verify_is_rejected(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["a.py"])
    session.task.items[0].verify = "false"

    async def verify(item_id, statement, command):
        return False, "failed"

    verdict = await CompletionGate.check(
        session.task.items[0], _ctx(tmp_path, session, verify=verify)
    )

    assert verdict.ok is False
    assert "verify failed" in verdict.reason


@pytest.mark.asyncio
async def test_a_delete_receipt_completes_a_missing_file(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["gone.py"])
    session.record_effect("gone.py", "", tool="bash", call_id="c1", kind="delete")

    verdict = await CompletionGate.check(session.task.items[0], _ctx(tmp_path, session))

    assert verdict.ok is True


@pytest.mark.asyncio
async def test_a_failed_verify_is_not_hidden_by_a_file_receipt(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = _task(["a.py"])
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.record_effect("a.py", file_version(target), tool="write", call_id="c1")
    session.task.items[0].verify = "false"

    async def verify(item_id, statement, command):
        return False, "exit 1"

    verdict = await CompletionGate.check(
        session.task.items[0], _ctx(tmp_path, session, verify=verify)
    )

    assert verdict.ok is False
    assert "verify failed" in verdict.reason
