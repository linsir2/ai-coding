"""Sub-agent truncation signals (R0-3, TDD): unfinished must say so.

A budget stop currently reads like a result: no ``truncated`` flag, no error
code (C1 recorded ``error=None`` on a timeout), no cost accounting, and zero
child events reach the parent (the runner's observer slot is never filled).
"""

import pytest

from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.subagent import SubagentResult
from ai_coding.tools.subagent_tool import SubAgentTool


def _result(status="completed", **over):
    base = {"status": status, "conclusion": "did things", "iterations": 3}
    base.update(over)
    return SubagentResult(**base)


def test_timeout_packet_marks_truncated_with_reason_and_usage():
    from ai_coding.domain.loop import TurnOutcome, TurnStatus

    runner = SubAgentRunner(
        app_config=None, engine_factory=lambda: None, build_tools=lambda wd: None,
        timeout_seconds=120.0,
    )
    outcome = TurnOutcome(status=TurnStatus.TIMEOUT, text="partial",
                          iterations=11, reason="turn deadline exceeded",
                          usage={"prompt_tokens": 100, "completion_tokens": 50})
    loop = _FakeLoop(outcome)
    packet = runner._packet(loop, _session(), None)  # noqa: SLF001 - packet shape is the contract
    assert packet.status == "timeout"
    assert packet.truncated is True
    assert packet.exit_reason is not None and "deadline" in packet.exit_reason
    assert packet.usage == {"prompt_tokens": 100, "completion_tokens": 50}


def test_completed_packet_is_not_truncated():
    from ai_coding.domain.loop import TurnOutcome, TurnStatus

    runner = SubAgentRunner(
        app_config=None, engine_factory=lambda: None, build_tools=lambda wd: None,
    )
    outcome = TurnOutcome(status=TurnStatus.COMPLETED, text="done", iterations=2)
    packet = runner._packet(_FakeLoop(outcome), _session(), None)  # noqa: SLF001
    assert packet.truncated is False
    assert packet.exit_reason is None


def test_ledger_file_keeps_the_new_fields(tmp_path):
    import json
    from pathlib import Path

    from ai_coding.core.subagent import _with_ledger  # noqa: SLF001

    result = _result(status="timeout", truncated=True, exit_reason="outer_timeout",
                     usage={"prompt_tokens": 1})
    ledged = _with_ledger(result, Path(str(tmp_path)))
    assert ledged.truncated is True
    assert ledged.exit_reason == "outer_timeout"
    assert ledged.usage == {"prompt_tokens": 1}
    on_disk = json.loads(Path(ledged.ledger_path).read_text())
    assert on_disk["truncated"] is True
    assert on_disk["exit_reason"] == "outer_timeout"


def test_tool_result_carries_machine_readable_child_codes():
    from ai_coding.tools.subagent_tool import _to_tool_result  # noqa: SLF001

    timed_out = _to_tool_result(_result(status="timeout", error="exceeded 25s"), "t")
    assert timed_out.success is False
    assert timed_out.error == "child_timeout"

    budgeted = _to_tool_result(_result(status="max_iterations"), "t")
    # Partial work still beats discarding it: success stays, but the code
    # travels with it so the parent can tell "done" from "cut".
    assert budgeted.success is True
    assert budgeted.error == "child_max_iterations"

    done = _to_tool_result(_result(status="completed"), "t")
    assert done.success is True and done.error is None


def test_tool_render_names_truncation_not_cost():
    """r25/v2: usage is report-only; the model report names the cut and the
    ledger pointer (the full packet), not tokens."""
    from ai_coding.tools.subagent_tool import _render  # noqa: SLF001

    text = _render(
        _result(status="timeout", truncated=True, exit_reason="outer_timeout",
                usage={"prompt_tokens": 100, "completion_tokens": 50},
                ledger_path="/tmp/ledger.json"), "t")
    assert "truncated" in text and "outer_timeout" in text
    assert "100" not in text and "50" not in text
    assert "完整回包" in text


@pytest.mark.asyncio
async def test_child_events_reach_the_parent_observer():
    """The runner's observer slot is filled per call from the tool context."""
    from ai_coding.core.events import ModelStepFinished, ScopedEvent
    from ai_coding.core.observers import ScopedObserver
    from ai_coding.tools.base import ToolContext

    seen: list = []

    class _ScopedCannedRunner(_CannedRunner):
        async def run(self, task, name="sub-agent", **kwargs):
            self.seen_observer = kwargs.get("observer")
            # Mimic SubAgentRunner._scoped_observer: the child loop wraps
            # whatever observer the call carries with its parentage.
            ScopedObserver(scope="subagent:probe:root", parent="subagent",
                           inner=self.seen_observer).on_event(
                ModelStepFinished(index=1))
            return self._result

    tool = SubAgentTool(_ScopedCannedRunner(_result()))
    result = await tool.execute({"task": "do it", "name": "probe"},
                                ToolContext(emit=seen.append))
    assert result.success is True
    assert len(seen) == 1 and isinstance(seen[0], ScopedEvent)


class _FakeLoop:
    def __init__(self, outcome):
        self._outcome = outcome
        self.last_artifacts = []

    @property
    def last_outcome(self):
        return self._outcome


def _session():
    from ai_coding.domain.session import SessionData

    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


class _CannedRunner:
    """Stands in for SubAgentRunner: records the observer it was given."""

    def __init__(self, result):
        self._result = result
        self.seen_observer = None
        self.timeout_seconds = 600.0
        self.context_mode = "full"

    async def run(self, task, name="sub-agent", **kwargs):
        self.seen_observer = kwargs.get("observer")
        return self._result
