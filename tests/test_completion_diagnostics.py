"""Batch 0/1: persisted effects version and diagnostic completion codes."""

from __future__ import annotations

import pytest

from ai_coding.core.completion import CompletionGate
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext
from ai_coding.tools.todo_write_tool import TodoWriteTool


def _session(*, files: list[str] | None = None) -> SessionData:
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = TaskState(goal="g")
    item = session.task.add("step", files=files or [])
    item.started_seq = 1
    return session


def _ctx(tmp_path, session, *, legacy=False) -> ToolContext:
    return ToolContext(
        task=session.task,
        workspace=str(tmp_path),
        effects=lambda: session.effects,
        effects_legacy=legacy,
        completion_override_approved=True,
    )


def test_new_sessions_persist_a_nonzero_effects_version():
    data = _session().to_dict()
    assert data["effectsVersion"] >= 2


def test_a_session_without_effects_version_is_legacy():
    session = SessionData.from_dict(
        {"sessionId": "s", "title": "", "createdTime": "t", "lastAccessTime": "t"}
    )
    assert session.effects_version == 0
    assert session.effects_legacy is True


@pytest.mark.asyncio
async def test_a_legacy_missing_receipt_gets_a_specific_code(tmp_path):
    session = _session(files=["a.py"])
    (tmp_path / "a.py").write_text("x", encoding="utf-8")

    verdict = await CompletionGate.check(
        session.task.items[0], _ctx(tmp_path, session, legacy=True)
    )

    assert verdict.ok is False
    assert verdict.code == "legacy_session_unverified"


@pytest.mark.asyncio
async def test_a_fresh_missing_receipt_is_not_called_legacy(tmp_path):
    session = _session(files=["a.py"])
    (tmp_path / "a.py").write_text("x", encoding="utf-8")

    verdict = await CompletionGate.check(session.task.items[0], _ctx(tmp_path, session))

    assert verdict.ok is False
    assert verdict.code == "completion_unverified"


@pytest.mark.asyncio
async def test_a_directory_in_files_has_its_own_code(tmp_path):
    session = _session(files=["src"])
    (tmp_path / "src").mkdir()

    verdict = await CompletionGate.check(session.task.items[0], _ctx(tmp_path, session))

    assert verdict.ok is False
    assert verdict.code == "directory_unsupported"


@pytest.mark.asyncio
async def test_todo_write_maps_the_directory_code(tmp_path):
    session = _session(files=["src"])
    (tmp_path / "src").mkdir()

    result = await TodoWriteTool().execute(
        {"action": "update", "id": "1", "status": "completed"},
        _ctx(tmp_path, session),
    )

    assert result.success is False
    assert result.detail == "directory_unsupported"


def test_head_era_effect_keys_are_not_misclassified_as_legacy():
    session = SessionData.from_dict(
        {
            "sessionId": "s",
            "title": "",
            "createdTime": "t",
            "lastAccessTime": "t",
            "fileReceipts": [],
            "effectSeq": 0,
        }
    )
    assert session.effects_version == 2
    assert session.effects_legacy is False


@pytest.mark.asyncio
async def test_legacy_completed_step_is_regated_instead_of_inherited(tmp_path):
    from ai_coding.tools.update_plan_tool import UpdatePlanTool

    session = _session()
    item = session.task.items[0]
    item.id = "alpha"
    item.status = "completed"
    item.files = ["a.py"]
    item.evidence = "receipt: a.py@v1"
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    ctx = _ctx(tmp_path, session, legacy=True)

    result = await UpdatePlanTool().execute(
        {"plan": [{"id": "alpha", "step": "step", "status": "completed"}]},
        ctx,
    )

    assert result.success is False
    assert result.detail == "legacy_session_unverified"


def test_legacy_marker_survives_new_effects_and_round_trip():
    session = SessionData.from_dict(
        {"sessionId": "s", "title": "", "createdTime": "t", "lastAccessTime": "t"}
    )
    assert session.effects_legacy is True
    session.note_read("a.py", 1, 10, 10, version="v1", call_id="c1")

    restored = SessionData.from_dict(session.to_dict())

    assert restored.effects_version == 0
    assert restored.effects_legacy is True


@pytest.mark.asyncio
async def test_a_headless_override_names_the_receipts_that_work(tmp_path):
    """R4-5: a headless run has no operator to grant an override.  Measured:
    the model retried `override=true` three times instead of adding `files`."""
    session = _session()
    ctx = ToolContext(
        task=session.task,
        workspace=str(tmp_path),
        effects=lambda: session.effects,
    )

    verdict = await CompletionGate.override(session.task.items[0], ctx, "because")

    assert verdict.ok is False
    assert verdict.code == "override_unavailable"
    assert "files" in verdict.reason and "verify" in verdict.reason
