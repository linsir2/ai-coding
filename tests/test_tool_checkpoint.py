"""TDD (R8a): `checkpoint_write` - the model's own line for the next turn.

Different from `memory_write`: durable vs turn-scoped, `.memory` vs the digest
ring, "next session" vs "next turn" (docs/r8-analysis.md 1.2 / 3.1).
"""

import pytest

from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.tools.base import ToolContext
from ai_coding.tools.checkpoint_tool import CheckpointTool


@pytest.mark.asyncio
async def test_r8_the_tool_hands_the_note_to_the_turn_and_echoes_it():
    seen: list[str] = []
    result = await CheckpointTool().execute(
        {"note": "下一步：补 runner 用例"}, ToolContext(note_checkpoint=seen.append)
    )
    assert result.success is True
    assert seen == ["下一步：补 runner 用例"]
    assert "下一步：补 runner 用例" in result.output
    assert result.error is None


@pytest.mark.asyncio
async def test_r8_a_missing_note_is_a_structured_error():
    result = await CheckpointTool().execute({}, ToolContext(note_checkpoint=lambda _t: None))
    assert result.success is False
    assert result.error == "missing_note"
    assert result.data == {"note": ""}


@pytest.mark.asyncio
async def test_r8_blank_notes_are_rejected_not_stored():
    seen: list[str] = []
    result = await CheckpointTool().execute(
        {"note": "   \n "}, ToolContext(note_checkpoint=seen.append)
    )
    assert result.success is False
    assert result.error == "missing_note"
    assert seen == []


@pytest.mark.asyncio
async def test_r8_the_tool_reports_when_the_turn_cannot_be_reached():
    """A sub-agent (or a bare unit call) has no turn book to write into."""
    result = await CheckpointTool().execute({"note": "n"}, ToolContext())
    assert result.success is False
    assert result.error == "checkpoint_unavailable"


def test_r8_the_gate_allows_it_and_it_may_run_parallel():
    tool = CheckpointTool()
    assert tool.parallel_safe is True
    decision = PermissionGate().check("checkpoint_write", {"note": "n"})
    assert decision.decision is PermissionDecision.ALLOW


def test_r8_the_description_says_how_it_differs_from_memory_write():
    description = CheckpointTool().description
    assert "memory_write" in description
    assert "session" in description
