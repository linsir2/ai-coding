"""Batch 2b: merge_agent records exact receipts only for a real landing."""

from __future__ import annotations

import pytest

from ai_coding.core.dispatch import DispatchOutcome
from ai_coding.core.turn_books import TurnBooks
from ai_coding.domain.message import ToolCall, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.infra.file_utils import file_version
from ai_coding.tools.base import ToolContext
from ai_coding.tools.merge_agent_tool import MergeAgentTool


class _Scheduler:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def merge(self, target, verify_cmd=None, *, session_id="", workspace=""):
        self.calls.append((target, verify_cmd, session_id, workspace))
        return dict(self.outcome)


def _ctx(tmp_path, session, *, note_effect=None):
    return ToolContext(
        workspace=str(tmp_path),
        effects=lambda: session.effects,
        note_effect=note_effect,
        call_id="m1",
    )


@pytest.mark.asyncio
async def test_merged_write_and_delete_record_exact_receipts(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    seen = []
    outcome = {
        "status": "merged",
        "file_changes": [
            {"path": "a.py", "kind": "write", "version": file_version(target)},
            {"path": "gone.py", "kind": "delete", "version": ""},
        ],
    }
    tool = MergeAgentTool(_Scheduler(outcome))

    result = await tool.execute(
        {"target": "run-1"},
        _ctx(
            session=session,
            tmp_path=tmp_path,
            note_effect=lambda *row, **kw: seen.append((row, kw)),
        ),
    )

    assert result.success is True
    assert [(row[2], row[3]) for row, _ in seen] == [
        ("merge_agent", "write"),
        ("merge_agent", "delete"),
    ]
    assert seen[0][1]["confidence"] == "exact"
    assert seen[1][0][1] == ""


@pytest.mark.asyncio
async def test_merge_skipped_and_conflict_record_nothing(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    seen = []
    for status in ("merge_skipped", "conflict", "dirty", "verify_failed"):
        await MergeAgentTool(_Scheduler({"status": status})).execute(
            {"target": "run-1"},
            _ctx(
                session=session,
                tmp_path=tmp_path,
                note_effect=lambda *row, **kw: seen.append(row),
            ),
        )
    assert seen == []


@pytest.mark.asyncio
async def test_a_stale_version_is_not_recorded(tmp_path):
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    seen = []
    outcome = {
        "status": "merged",
        "file_changes": [{"path": "a.py", "kind": "write", "version": "old"}],
    }
    (tmp_path / "a.py").write_text("new", encoding="utf-8")

    await MergeAgentTool(_Scheduler(outcome)).execute(
        {"target": "run-1"},
        _ctx(session=session, tmp_path=tmp_path, note_effect=lambda *row, **kw: seen.append(row)),
    )

    assert seen == []


def test_merge_agent_is_an_action_only_after_merged():
    books = TurnBooks()
    call = ToolCall(id="c1", name="merge_agent")
    books.search_streak = 2
    books.note_outcomes([
        DispatchOutcome(
            call=call,
            result=ToolResult(success=True, data={"status": "merge_skipped"}),
        )
    ])
    assert books.search_streak == 2

    books.note_outcomes([
        DispatchOutcome(
            call=call,
            result=ToolResult(success=True, data={"status": "merged"}),
        )
    ])
    assert books.search_streak == 0
