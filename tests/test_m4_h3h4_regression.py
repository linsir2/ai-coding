"""H3/H4 regression pins for Task 6 (M4 fix wave).

H3: dispatch denied semantics for credential reads.
H4: queued posture survives reload via the pending-posture sidecar.
"""

from __future__ import annotations

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef
from ai_coding.security.approval import ApprovalCallback, ApprovalResult, AutoApproveCallback
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import ToolContext
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.registry import ToolRegistry


class _HumanApproval(ApprovalCallback):
    is_human = True

    def __init__(self, answers: list[ApprovalResult]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    async def approve(self, tool_name, params, reason):
        self.asked.append(tool_name)
        return self.answers.pop(0)


@pytest.mark.asyncio
async def test_credential_refusal_marks_denied_without_halt(tmp_path):
    """A credential read refused by auto-approval is denied, not halted."""
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))
    registry = ToolRegistry()
    registry.register(ReadTool(Sandbox(tmp_path), FileStateTracker()))
    dispatcher = ToolDispatcher(registry, gate, AutoApproveCallback())
    call = ToolCallRef(id="c1", name="read", arguments='{"path":".env"}')

    outcome = (
        await dispatcher.dispatch_batch(
            [call], ToolContext(workspace=str(tmp_path), mode="normal")
        )
    )[0]

    assert outcome.result.error == "credential_approval_required"
    assert outcome.denied is True
    # Not a halt: the batch ends normally, and the refusal carries no secret.
    assert outcome.halted is False
    assert "TOKEN=secret" not in outcome.result.output


@pytest.mark.asyncio
async def test_human_approval_clears_denied_for_the_approved_call(tmp_path):
    """A human YES for this one call runs it with denied=False."""
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))
    registry = ToolRegistry()
    registry.register(ReadTool(Sandbox(tmp_path), FileStateTracker()))
    approval = _HumanApproval([ApprovalResult.YES])
    dispatcher = ToolDispatcher(registry, gate, approval)
    call = ToolCallRef(id="c1", name="read", arguments='{"path":".env"}')

    outcome = (
        await dispatcher.dispatch_batch(
            [call], ToolContext(workspace=str(tmp_path), mode="normal")
        )
    )[0]

    assert outcome.result.success is True
    assert "TOKEN=secret" in outcome.result.output
    assert outcome.denied is False
    assert outcome.halted is False


@pytest.mark.asyncio
async def test_queued_posture_survives_reload_via_sidecar(tmp_path):
    """Queued agent flips are durable: reload still sees the pending posture.

    ``session_agent_select`` deliberately skips ``_persist_session`` for the
    queued case; durability comes from the pending-posture sidecar that
    ``request_posture`` writes (``agent_loop.py``) and ``SessionService.load``
    reads back.
    """
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.session import SessionData
    from ai_coding.service.session_service import SessionService

    sessions = SessionService(root=tmp_path / "sessions")
    session = sessions.create(title="h4")
    session.session_id = "h4sess01"
    sessions.save(session)

    # Durability comes from the pending-posture sidecar that
    # ``AgentLoop.request_posture`` writes (``agent_loop.py:525-526``) and
    # ``SessionService.load`` reads back (``session_service.py:291``).
    # ``handlers.session_agent_select`` deliberately skips ``_persist_session``
    # for the queued case, so this sidecar is the only durable copy.
    queued = {"agent": "plan", "readonly": True, "reason": "agent.select"}
    session.pending_posture = dict(queued)
    sessions.mark_pending_posture(session.session_id, queued)

    reloaded = sessions.load(session.session_id)
    assert reloaded is not None
    assert reloaded.pending_posture == queued
