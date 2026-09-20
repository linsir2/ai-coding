"""TDD (M2-3 / M3-1): AgentLoop with tool calls — multi-turn persistence.

Uses FakeAIService that simulates tool_calls and (M3) tool_outputs in TurnResult.
"""

import pytest

from ai_coding.ai.base import AIService
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.message import ToolCallRef, ToolOutput
from ai_coding.domain.run import TurnResult
from ai_coding.service.session_service import SessionService


class FakeAIToolService(AIService):
    """Fake that returns a turn with tool calls, optional outputs, and one text response."""

    def __init__(
        self,
        tool_calls: list[ToolCallRef],
        final_text: str = "done",
        tool_outputs: list[ToolOutput] | None = None,
    ) -> None:
        self._tool_calls = tool_calls
        self._final_text = final_text
        self._tool_outputs = tool_outputs or []
        self.last_history: list | None = None

    async def execute_turn(self, history, user_input, token_sink=None):
        self.last_history = list(history)
        return TurnResult(
            text=self._final_text,
            tool_calls=list(self._tool_calls),
            tool_outputs=list(self._tool_outputs),
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


@pytest.mark.asyncio
async def test_tool_outputs_persist_as_tool_messages_wrapped(tmp_path):
    # M3-1: when the engine surfaces tool outputs, AgentLoop persists them as
    # strict-pairing ``role=tool`` messages wrapped in the <tool_output> envelope.
    refs = [ToolCallRef(id="c1", name="read", arguments='{"path":"f.txt"}')]
    outputs = [ToolOutput(call_id="c1", tool_name="read", output="hello file")]
    ai = FakeAIToolService(refs, "saw it", tool_outputs=outputs)
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    session = loop.sessions.create()

    await loop.process_input(session, "read f.txt")

    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant", "tool"]
    tool_msg = session.messages[2]
    assert tool_msg.tool_call_id == "c1"
    assert tool_msg.tool_name == "read"
    assert "<tool_output tool=\"read\">" in tool_msg.content
    assert "hello file" in tool_msg.content


@pytest.mark.asyncio
async def test_tool_calls_survive_pairing_when_outputs_present(tmp_path):
    # M3-1: with outputs present, the assistant's tool_calls stay paired (not stripped).
    refs = [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')]
    outputs = [ToolOutput(call_id="c1", tool_name="bash", output="a.txt")]
    ai = FakeAIToolService(refs, "ok", tool_outputs=outputs)
    loop = AgentLoop(ai, SessionService(tmp_path / "sessions"))
    session = loop.sessions.create()

    await loop.process_input(session, "ls")

    assistant = session.messages[1]
    assert assistant.tool_calls == list(refs)
    # and the tool message references the same id -> pairing holds
    assert session.messages[2].tool_call_id == "c1"


@pytest.mark.asyncio
async def test_session_round_trip_keeps_tool_messages(tmp_path):
    # M3-1: persisted session reloads the tool messages (Java-compatible shape).
    refs = [ToolCallRef(id="c1", name="glob", arguments='{"pattern":"*.py"}')]
    outputs = [ToolOutput(call_id="c1", tool_name="glob", output="a.py")]
    ai = FakeAIToolService(refs, "found", tool_outputs=outputs)
    svc = SessionService(tmp_path / "sessions")
    loop = AgentLoop(ai, svc)
    session = svc.create()
    await loop.process_input(session, "glob")

    reloaded = svc.load(session.session_id)
    assert reloaded is not None
    roles = [m.role for m in reloaded.messages]
    assert roles == ["user", "assistant", "tool"]
    assert reloaded.messages[2].tool_call_id == "c1"
