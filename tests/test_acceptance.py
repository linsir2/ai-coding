"""Proof command runner (R0-1, TDD): run one verification command, bounded.

``verify_items`` is the one shell boundary the ``update_plan`` verify path
uses: one command in, ``(passed, output tail)`` out.  The rules, the workspace
boundary and the human approval still apply to it.
"""

import pytest

from ai_coding.core.acceptance import verify_items
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import ApprovalResult


@pytest.mark.asyncio
async def test_verify_items_runs_one_command_and_reports_its_verdict(tmp_path):
    passed = await verify_items(["echo hi"], str(tmp_path))
    assert len(passed) == 1
    assert passed[0] == (True, "hi")

    failed = await verify_items(["ls /definitely-not-here-xyz"], str(tmp_path))
    assert failed[0][0] is False
    assert failed[0][1] != ""


@pytest.mark.asyncio
async def test_verify_times_out_a_hung_command(tmp_path):
    result = await verify_items(["sleep 30"], str(tmp_path), timeout_each=0.3)
    assert result[0][0] is False
    assert "timeout" in result[0][1].lower()


@pytest.mark.asyncio
async def test_a_command_left_unchecked_by_the_total_budget_fails_closed(tmp_path):
    """No room left means the command did not run, which is never a pass."""
    result = await verify_items(
        ["touch budget.marker"], str(tmp_path), timeout_total=0.0
    )
    assert result[0] == (False, "not checked: time budget exhausted")
    assert not (tmp_path / "budget.marker").exists()


# ---- 18 §10.5 (R4b): a read-only session never runs the proof shell ----------


def _acceptance_loop(tmp_path):
    from ai_coding.ai.engine import ChatEngine
    from ai_coding.config.models import AIConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.loop import StepResult
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry

    class _Text(ChatEngine):
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return StepResult(text="done", finish_reason="stop")

    return AgentLoop(
        _Text(),
        SessionService(root=tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        workspace=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_a_readonly_session_never_runs_the_proof_command(tmp_path):
    """``verify_items`` runs ``shell=True`` and bypasses the dispatch gate — a
    read-only session must not execute the plan item's proof."""
    loop = _acceptance_loop(tmp_path)
    session = SessionData(
        session_id="s",
        title="",
        created_time="t",
        last_access_time="t",
        readonly=True,
    )

    verdict = await loop._verify_plan_item(session, "1", "tests pass", "touch verified.marker")

    assert not (tmp_path / "verified.marker").exists()
    assert verdict == (False, "the proof command was not run")
    await loop.aclose()


@pytest.mark.asyncio
async def test_a_writable_session_still_runs_the_proof_command(tmp_path):
    """The guard must not over-suppress: a writable session still proves."""
    loop = _acceptance_loop(tmp_path)
    session = SessionData(
        session_id="s",
        title="",
        created_time="t",
        last_access_time="t",
    )

    verdict = await loop._verify_plan_item(session, "1", "tests pass", "touch verified.marker")

    assert (tmp_path / "verified.marker").exists()
    assert verdict[0] is True
    await loop.aclose()


# ---- W2: a proof command is a shell command, and shells have no sandbox -----


class _RecordingApproval:
    """A human who answers, and a record of what they were asked."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict, str]] = []

    async def approve(self, tool_name, params, reason):
        self.asked.append((tool_name, dict(params), reason))
        return self.answer


@pytest.mark.asyncio
async def test_a_proof_command_inside_the_workspace_runs_without_asking(tmp_path):
    """The common case must not cost a click: a proof command that only
    touches the workspace is what a proof command is for."""
    approval = _RecordingApproval(ApprovalResult.NO)

    result = await verify_items(["touch inside.marker"], str(tmp_path), approval=approval)

    assert result[0][0] is True
    assert (tmp_path / "inside.marker").exists()
    assert approval.asked == []


@pytest.mark.asyncio
async def test_a_proof_command_reaching_outside_asks_first(tmp_path):
    """A proof command that names a path outside the workspace is asking
    for something the file tools could not do.  It runs only if the human
    says so."""
    outside = tmp_path.parent / "w2_proof_outside.txt"
    approval = _RecordingApproval(ApprovalResult.YES)

    result = await verify_items(
        [f"printf x > {outside} && test -f {outside}"],
        str(tmp_path),
        approval=approval,
    )

    assert len(approval.asked) == 1
    assert result[0][0] is True
    assert outside.exists()


@pytest.mark.asyncio
async def test_a_refused_proof_command_does_not_run(tmp_path):
    """The refusal has to be a refusal: the command never runs, and the item
    fails rather than passing on a file that was never written."""
    outside = tmp_path.parent / "w2_proof_refused.txt"
    approval = _RecordingApproval(ApprovalResult.NO)

    result = await verify_items(
        [f"printf x > {outside} && test -f {outside}"],
        str(tmp_path),
        approval=approval,
    )

    assert len(approval.asked) == 1
    assert not outside.exists()
    assert result[0][0] is False


@pytest.mark.asyncio
async def test_a_proof_command_reaching_outside_through_a_variable_asks(tmp_path):
    """W2/T: the path is in the assignment, not in the command that uses it.

    ``D=/data/x; rm -rf $D`` names a path the file tools cannot reach, and a
    text scan cannot see it -- the variable hides it.  The shell grammar can:
    the assignment is a word of its own.
    """
    approval = _RecordingApproval(ApprovalResult.NO)

    result = await verify_items(
        ["D=/data/w2proof; rm -rf $D"],
        str(tmp_path),
        approval=approval,
    )

    assert len(approval.asked) == 1, "the variable hid the path from the gate"
    assert result[0][0] is False
