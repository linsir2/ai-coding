"""Acceptance contracts (R0-1, TDD): done-ness you verify, not claim.

An acceptance item is one machine-checkable statement plus the shell command
that checks it.  The loop runs the command (bounded); the policy only ever
sees the result, so ``TurnPolicy`` stays pure.
"""

import pytest

from ai_coding.core.acceptance import verify_items
from ai_coding.domain.acceptance import (
    AcceptanceItem,
    acceptance_states,
    failed_items,
    pending_items,
)
from ai_coding.domain.session import SessionData


def _item(**over):
    base = {"id": "AC-1", "statement": "tests pass", "verify": "true"}
    base.update(over)
    return AcceptanceItem(**base)


def test_item_round_trips_through_the_session():
    session = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t",
        acceptance=[_item(verify="pytest tests/ -q")],
    )
    data = session.to_dict()
    assert data["acceptance"] == [{
        "id": "AC-1", "statement": "tests pass", "verify": "pytest tests/ -q",
        "passed": None, "output": "",
    }]
    assert SessionData.from_dict(data).acceptance == session.acceptance


def test_empty_acceptance_leaves_no_key_for_the_java_reader():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    assert "acceptance" not in session.to_dict()


def test_pending_and_failed_helpers():
    items = [
        _item(id="AC-1", passed=True),
        _item(id="AC-2", passed=False),
        _item(id="AC-3", passed=None),
    ]
    assert [i.id for i in pending_items(items)] == ["AC-2", "AC-3"]
    assert [i.id for i in failed_items(items)] == ["AC-2"]
    assert [s.id for s in acceptance_states(items)] == ["AC-1", "AC-2", "AC-3"]
    assert acceptance_states(items)[0].passed is True


@pytest.mark.asyncio
async def test_verify_passes_true_and_fails_false(tmp_path):
    passed = await verify_items([_item(id="AC-1", verify="echo hi")], str(tmp_path))
    assert passed[0].passed is True
    assert passed[0].output == "hi"
    failed = await verify_items(
        [_item(id="AC-2", verify="ls /definitely-not-here-xyz")], str(tmp_path)
    )
    assert failed[0].passed is False
    assert failed[0].output != ""


@pytest.mark.asyncio
async def test_verify_times_out_a_hung_command(tmp_path):
    items = await verify_items(
        [_item(id="AC-9", verify="sleep 30")], str(tmp_path), timeout_each=0.3
    )
    assert items[0].passed is False
    assert "timeout" in items[0].output.lower()


@pytest.mark.asyncio
async def test_verify_skips_already_passed_items(tmp_path):
    """Re-running pytest every iteration would burn the wall clock."""
    items = await verify_items(
        [_item(id="AC-1", verify="false", passed=True)], str(tmp_path)
    )
    assert items[0].passed is True


@pytest.mark.asyncio
async def test_budget_stop_with_green_suite_is_completed_not_max(tmp_path):
    """H1-01 replay: the budget ran out with the work done — the verdict
    must say COMPLETED, not MAX_ITERATIONS."""
    from ai_coding.ai.engine import ChatEngine
    from ai_coding.config.models import AIConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.core.events import RecordingObserver, TurnFinished
    from ai_coding.domain.loop import StepResult, TurnStatus
    from ai_coding.domain.message import ToolCallRef, ToolResult
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.base import BaseTool
    from ai_coding.tools.registry import ToolRegistry

    class _Engine(ChatEngine):
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return StepResult(
                text=None, finish_reason="tool_calls",
                tool_calls=[ToolCallRef(id="c1", name="read", arguments="{}")],
            )

    class _Read(BaseTool):
        @property
        def name(self):
            return "read"

        @property
        def description(self):
            return "fake"

        @property
        def params_json_schema(self):
            return {"type": "object", "properties": {}}

        async def execute(self, params, ctx=None):
            return ToolResult(success=True, output="ok")

    registry = ToolRegistry()
    registry.register(_Read())
    loop = AgentLoop(
        _Engine(), SessionService(root=tmp_path / "sessions"), registry,
        settings=AIConfig(stop_policy="natural", max_tool_iterations=1),
        workspace=str(tmp_path),
    )
    session = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t",
        acceptance=[_item(id="AC-1", verify="true")],
    )
    rec = RecordingObserver()
    await loop.process_input(session, "go", observer=rec)
    outcome = [e for e in rec.events if isinstance(e, TurnFinished)][0].outcome
    assert outcome.status is TurnStatus.COMPLETED
    assert outcome.reason == "acceptance verified (post-budget)"
    await loop.aclose()


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
async def test_a_readonly_session_never_runs_the_proof_commands(tmp_path):
    """``verify_items`` runs ``shell=True`` and bypasses the dispatch gate — a
    read-only session must not execute it at turn end (17 missed this hole)."""
    loop = _acceptance_loop(tmp_path)
    session = SessionData(
        session_id="s",
        title="",
        created_time="t",
        last_access_time="t",
        readonly=True,
        acceptance=[_item(verify="touch verified.marker")],
    )

    await loop.process_input(session, "go")

    assert not (tmp_path / "verified.marker").exists()
    assert session.acceptance[0].passed is None
    assert loop.last_outcome is not None
    # R08: a read-only (non-plan) turn finishes quietly on the first stop --
    # the submission guard speaks to plan posture only.  The hole this test
    # pins (proof commands must never run here) is unchanged.
    assert loop.last_outcome.reason == "model finished"
    await loop.aclose()


@pytest.mark.asyncio
async def test_a_writable_session_still_runs_the_proof_commands(tmp_path):
    """The guard must not over-suppress: a writable session still proves."""
    loop = _acceptance_loop(tmp_path)
    session = SessionData(
        session_id="s",
        title="",
        created_time="t",
        last_access_time="t",
        acceptance=[_item(verify="touch verified.marker")],
    )

    await loop.process_input(session, "go")

    assert (tmp_path / "verified.marker").exists()
    assert session.acceptance[0].passed is True
    await loop.aclose()
