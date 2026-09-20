"""TDD (M2-3): AgentLoop with tool calls — multi-turn persistence.

Uses FakeAIService that simulates tool_calls in TurnResult.
"""

import pytest

from ai_coding.ai.base import AIService
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.run import TurnResult
from ai_coding.service.session_service import SessionService


class FakeAIToolService(AIService):
    """Fake that returns a turn with one tool call and one text response."""

    def __init__(self, tool_calls: list[ToolCallRef], final_text: str = "done") -> None:
        self._tool_calls = tool_calls
        self._final_text = final_text
        self.last_history: list | None = None

    async def execute_turn(self, history, user_input, token_sink=None):
        self.last_history = list(history)
        return TurnResult(
            text=self._final_text,
            tool_calls=list(self._tool_calls),
        )


@pytest.mark.asyncio
async def test_process_input_with_tool_calls_persists_both(tmp_path):
    tool_refs = [ToolCallRef(id="call_1", name="read", arguments='{"path":"f.txt"}')]
    ai = FakeAIToolService(tool_refs, final_text="Here is the file content.")
    svc = SessionService(tmp_path / "sessions")
    loop = AgentLoop(ai, svc)

    session = svc.create()
    text = await loop.process_input(session, "read f.txt")

    assert text == "Here is the file content."
    # user + assistant (with tool_calls) should be in session
    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant"]
    assistant_msg = session.messages[1]
    assert assistant_msg.tool_calls is not None
    assert len(assistant_msg.tool_calls) == 1
    assert assistant_msg.tool_calls[0].name == "read"
    assert assistant_msg.tool_calls[0].id == "call_1"


@pytest.mark.asyncio
async def test_tool_call_arguments_preserved(tmp_path):
    tool_refs = [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')]
    ai = FakeAIToolService(tool_refs, "ok")
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    session = loop.sessions.create()

    await loop.process_input(session, "list files")

    assistant = session.messages[1]
    assert assistant.tool_calls[0].arguments == '{"command":"ls"}'


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_one_turn(tmp_path):
    refs = [
        ToolCallRef(id="a", name="read", arguments='{"path":"a.txt"}'),
        ToolCallRef(id="b", name="read", arguments='{"path":"b.txt"}'),
    ]
    ai = FakeAIToolService(refs, "both files read")
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    session = loop.sessions.create()

    await loop.process_input(session, "read a and b")

    assistant = session.messages[1]
    assert len(assistant.tool_calls) == 2
    assert assistant.tool_calls[0].id == "a"
    assert assistant.tool_calls[1].id == "b"


@pytest.mark.asyncio
async def test_empty_tool_calls_still_works(tmp_path):
    ai = FakeAIToolService([], "hello")
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    session = loop.sessions.create()

    text = await loop.process_input(session, "hi")
    assert text == "hello"
    assert len(session.messages) == 2
    assert session.messages[1].tool_calls is None


@pytest.mark.asyncio
async def test_tool_results_are_not_added_by_fake(tmp_path):
    # M2-3: SDK runs tools internally; we only collect them from stream events.
    # FakeAIService doesn't produce tool outputs, so no tool messages expected.
    refs = [ToolCallRef(id="c1", name="read", arguments='{}')]
    ai = FakeAIToolService(refs, "done")
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    session = loop.sessions.create()

    await loop.process_input(session, "x")
    # No tool role messages — SDK adapter handles tool execution.
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert len(tool_msgs) == 0
