"""End-to-end: a real write and todo_write in one batch share the effect ledger."""

from __future__ import annotations

import pytest

from ai_coding.domain.message import ToolResult
from ai_coding.domain.task import TaskState
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.service.session_service import SessionService
from ai_coding.tools.todo_write_tool import TodoWriteTool
from ai_coding.tools.write_tool import WriteTool
from tests.test_agent_loop_react import (
    ScriptedEngine,
    _calls,
    _loop,
    _ref,
    _registry,
    _settings,
    _text,
    _Tool,
)


@pytest.mark.asyncio
async def test_a_write_then_todo_completion_in_the_same_batch_sees_the_receipt(tmp_path):
    writer = WriteTool(Sandbox(tmp_path), FileStateTracker())
    engine = ScriptedEngine(
        [
            _calls(
                _ref("w1", "write", '{"path":"a.py","content":"x"}'),
                _ref(
                    "t1",
                    "todo_write",
                    '{"action":"update","id":"1","status":"completed"}',
                ),
            ),
            _text("done"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(
        engine,
        sessions,
        _registry(writer, TodoWriteTool()),
        settings=_settings(),
        workspace=str(tmp_path),
        approval=AutoApproveCallback(),
    )
    session = sessions.create()
    session.task = TaskState(goal="g")
    item = session.task.add("create a.py", files=["a.py"])
    item.started_seq = 1

    await loop.process_input(session, "go")

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x"
    assert session.task.items[0].status == "completed"
    assert session.effects.receipt_for("a.py") is not None


@pytest.mark.asyncio
async def test_a_subagent_changed_path_becomes_a_parent_receipt(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    child = _Tool(
        "subAgent",
        result=ToolResult(success=True, output="done", data={"changed_paths": ["a.py"]}),
    )
    engine = ScriptedEngine([_calls(_ref("s1", "subAgent")), _text("done")])
    loop = _loop(
        engine,
        SessionService(tmp_path / "sessions"),
        _registry(child),
        settings=_settings(),
        workspace=str(tmp_path),
        approval=AutoApproveCallback(),
    )
    session = loop.sessions.create()

    await loop.process_input(session, "go")

    receipt = session.effects.receipt_for("a.py")
    assert receipt is not None
    assert receipt.tool == "subAgent"
    assert receipt.call_id == "s1"
    assert receipt.confidence == "unknown"
    assert receipt.version == ""
