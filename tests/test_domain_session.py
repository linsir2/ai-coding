from ai_coding.domain.message import ChatMessage, ToolCallRef
from ai_coding.domain.session import SessionData


def _user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text, timestamp="t")


def test_session_add_message_appends():
    s = SessionData("s1", "title", "c", "l")
    s.add_message(_user("hello"))
    s.add_message(_user("world"))
    assert len(s.messages) == 2


def test_session_to_dict_from_dict_roundtrip():
    s = SessionData("s1", "title", "c", "l")
    s.add_message(_user("hi"))
    assistant = ChatMessage(
        role="assistant",
        content=None,
        timestamp="t",
        tool_calls=[ToolCallRef(id="1", name="read", arguments='{"path":"a"}')],
    )
    s.add_message(assistant)
    s.add_message(ChatMessage(role="tool", content="out", timestamp="t", tool_call_id="1"))

    restored = SessionData.from_dict(s.to_dict())
    assert restored.session_id == "s1"
    assert len(restored.messages) == 3
    assert restored.messages[0].role == "user"
    assert restored.messages[1].tool_calls is not None
    assert restored.messages[1].tool_calls[0].id == "1"
    assert restored.messages[2].tool_call_id == "1"


def test_session_from_dict_java_shape():
    java_json = {
        "sessionId": "abc",
        "title": "java",
        "createdTime": "t0",
        "lastAccessTime": "t1",
        "messages": [
            {"role": "user", "content": "hi", "timestamp": "t0"},
            {
                "role": "assistant",
                "content": None,
                "timestamp": "t0",
                "toolCalls": [{"id": "9", "name": "bash", "arguments": '{"command":"ls"}'}],
            },
            {
                "role": "tool",
                "content": "out",
                "timestamp": "t0",
                "toolCallId": "9",
                "toolName": "bash",
            },
        ],
    }
    s = SessionData.from_dict(java_json)
    assert len(s.messages) == 3
    assert s.messages[1].tool_calls[0].arguments == '{"command":"ls"}'
    assert s.messages[2].role == "tool"
    assert s.messages[2].tool_call_id == "9"


def test_session_to_dict_java_shaped_keys():
    s = SessionData("abc", "t", "c", "l")
    s.add_message(_user("hi"))
    d = s.to_dict()
    assert "sessionId" in d
    assert "createdTime" in d
    assert "toolCallId" not in d  # no tool messages yet


def test_ensure_tool_pairing_drops_orphans():
    s = SessionData("s", "t", "c", "l")
    assistant = ChatMessage(
        role="assistant",
        content=None,
        timestamp="t",
        tool_calls=[ToolCallRef(id="a", name="read", arguments="{}")],
    )
    orphan_tool = ChatMessage(role="tool", content="x", timestamp="t", tool_call_id="z")
    paired = ChatMessage(role="tool", content="y", timestamp="t", tool_call_id="a")
    s.messages = [assistant, orphan_tool, paired]
    s.ensure_tool_pairing()
    ids = [(m.role, m.tool_call_id) for m in s.messages]
    assert ("tool", "a") in ids
    assert ("tool", "z") not in ids
    assert all(m.tool_calls is not None for m in s.messages if m.role == "assistant")
