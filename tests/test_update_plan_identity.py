"""update_plan step identity: id first, exact content second, never position."""

from __future__ import annotations

import pytest

from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext
from ai_coding.tools.update_plan_tool import UpdatePlanTool


def _ctx(session: SessionData, tmp_path) -> ToolContext:
    return ToolContext(
        task=session.task,
        workspace=str(tmp_path),
        effects=lambda: session.effects,
        completion_override_approved=True,
    )


def _session(*items: tuple[str, str]) -> SessionData:
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = TaskState(goal="g")
    for item_id, content in items:
        item = session.task.add(content, item_id=item_id)
        item.started_seq = 1
    return session


@pytest.mark.asyncio
async def test_position_does_not_inherit_completed_proof(tmp_path):
    session = _session(("1", "old step"))
    old = session.task.items[0]
    old.status = "completed"
    old.files = ["a.py"]
    old.evidence = "receipt: a.py@v1"

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"step": "different step", "status": "completed"},
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is False
    assert result.error == "completion_unverified"
    assert session.task.items[0].content == "old step"


@pytest.mark.asyncio
async def test_id_preserves_proof_after_reorder(tmp_path):
    session = _session(("alpha", "step A"), ("beta", "step B"))
    first = session.task.items[0]
    first.status = "completed"
    first.files = ["a.py"]
    first.evidence = "receipt: a.py@v1"

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"id": "beta", "step": "step B", "status": "pending"},
                {
                    "id": "alpha",
                    "step": "step A",
                    "status": "completed",
                },
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is True
    assert [item.id for item in session.task.items] == ["beta", "alpha"]
    assert session.task.items[1].files == ["a.py"]
    assert session.task.items[1].evidence == "receipt: a.py@v1"


@pytest.mark.asyncio
async def test_id_preserves_proof_after_rephrase(tmp_path):
    session = _session(("alpha", "step A"))
    prior = session.task.items[0]
    prior.status = "completed"
    prior.files = ["a.py"]
    prior.evidence = "receipt: a.py@v1"

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"id": "alpha", "step": "step A, rephrased", "status": "completed"}
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is True
    assert session.task.items[0].content == "step A, rephrased"
    assert session.task.items[0].files == ["a.py"]


@pytest.mark.asyncio
async def test_duplicate_step_ids_are_refused(tmp_path):
    session = _session()

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"id": "x", "step": "one", "status": "pending"},
                {"id": "x", "step": "two", "status": "pending"},
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is False
    assert result.error == "invalid_plan"


@pytest.mark.asyncio
async def test_completed_without_matched_proof_is_regated(tmp_path):
    session = _session(("alpha", "step A"))
    prior = session.task.items[0]
    prior.status = "completed"
    prior.evidence = "done"

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"id": "alpha", "step": "step A", "status": "completed"}
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is False
    assert result.error == "completion_unverified"
    assert session.task.items[0].status != "completed" or session.task.items[0].evidence == "done"


@pytest.mark.asyncio
async def test_mixed_explicit_and_content_ids_do_not_collide(tmp_path):
    session = _session(("1", "A"), ("2", "B"))
    session.task.items[0].status = "completed"
    session.task.items[0].evidence = "override: fixture"

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"step": "new X", "status": "pending"},
                {"id": "2", "step": "B renamed", "status": "pending"},
                {"step": "A", "status": "completed"},
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is True
    ids = [item.id for item in session.task.items]
    assert len(ids) == len(set(ids)) == 3
    assert ids[2] == "1"


@pytest.mark.asyncio
async def test_two_entries_matching_the_same_prior_are_refused(tmp_path):
    session = _session(("1", "A"))

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {"step": "A", "status": "pending"},
                {"step": "A", "status": "pending"},
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is False
    assert result.error == "invalid_plan"


@pytest.mark.asyncio
async def test_a_proven_completed_step_ignores_redundant_override(tmp_path):
    session = _session(("alpha", "A"))
    prior = session.task.items[0]
    prior.status = "completed"
    prior.files = ["a.py"]
    prior.evidence = "receipt: a.py@v1"

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {
                    "id": "alpha",
                    "step": "A",
                    "status": "completed",
                    "override": True,
                    "override_reason": "redundant",
                }
            ]
        },
        _ctx(session, tmp_path),
    )

    assert result.success is True
    assert session.task.items[0].evidence == "receipt: a.py@v1"


@pytest.mark.asyncio
async def test_work_done_before_in_progress_still_counts_for_completion(tmp_path):
    from ai_coding.infra.file_utils import file_version

    session = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t"
    )
    session.task = TaskState(goal="g")
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    ctx = ToolContext(
        task=session.task,
        workspace=str(tmp_path),
        effects=lambda: session.effects,
    )

    initial = await UpdatePlanTool().execute(
        {
            "plan": [
                {
                    "id": "s",
                    "step": "create a.py",
                    "status": "pending",
                    "files": ["a.py"],
                }
            ]
        },
        ctx,
    )
    assert initial.success is True

    # The model writes before flipping the step to in_progress.
    session.effects = session.effects.record_effect(
        "a.py", file_version(target), tool="write", call_id="c1"
    )
    started = await UpdatePlanTool().execute(
        {
            "plan": [
                {
                    "id": "s",
                    "step": "create a.py",
                    "status": "in_progress",
                    "files": ["a.py"],
                }
            ]
        },
        ctx,
    )
    assert started.success is True

    completed = await UpdatePlanTool().execute(
        {
            "plan": [
                {
                    "id": "s",
                    "step": "create a.py",
                    "status": "completed",
                    "files": ["a.py"],
                }
            ]
        },
        ctx,
    )
    assert completed.success is True, completed.output
    assert session.task.items[0].started_seq <= 1


@pytest.mark.asyncio
async def test_an_old_started_boundary_can_be_lowered_to_a_real_receipt(tmp_path):
    from ai_coding.infra.file_utils import file_version

    session = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t"
    )
    session.task = TaskState(goal="g")
    item = session.task.add(
        "create a.py", files=["a.py"], item_id="s"
    )
    item.started_seq = 5
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    session.effects = session.effects.record_effect(
        "a.py", file_version(target), tool="write", call_id="c1", seq=3
    )
    ctx = ToolContext(
        task=session.task,
        workspace=str(tmp_path),
        effects=lambda: session.effects,
    )

    result = await UpdatePlanTool().execute(
        {
            "plan": [
                {
                    "id": "s",
                    "step": "create a.py",
                    "status": "completed",
                    "files": ["a.py"],
                }
            ]
        },
        ctx,
    )

    assert result.success is True, result.output
