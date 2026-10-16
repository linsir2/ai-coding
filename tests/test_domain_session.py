from ai_coding.domain.message import ChatMessage, ToolCallRef
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState


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


def test_old_session_without_turn_usages_loads_empty():
    s = SessionData.from_dict({"sessionId": "old", "messages": []})
    assert s.turn_usages == []
    assert "turnUsages" not in s.to_dict()


def test_record_turn_usage_appends_row_and_roundtrips():
    from ai_coding.domain.session import TurnUsage

    s = SessionData("s1", "title", "c", "l")
    s.record_turn_usage(2, {"prompt_tokens": 100, "completion_tokens": 50,
                            "total_tokens": 150})
    s.record_turn_usage(3, None)
    s.record_turn_usage(4, {"prompt_tokens": 0, "completion_tokens": 0,
                            "total_tokens": 0})
    assert s.turn_usages == [
        TurnUsage(turn=2, prompt_tokens=100, completion_tokens=50, total_tokens=150)
    ]
    restored = SessionData.from_dict(s.to_dict())
    assert restored.turn_usages == s.turn_usages


def test_turn_usage_from_dict_rejects_bad_rows():
    from ai_coding.domain.session import TurnUsage

    assert TurnUsage.from_dict(None) is None
    assert TurnUsage.from_dict({"turn": 0}) is None
    assert TurnUsage.from_dict({"turn": "x"}) is None
    row = TurnUsage.from_dict({"turn": 1})
    assert row is not None and row.prompt_tokens == 0


def test_begin_plan_resets_goal_items_and_receipts():
    """A new plan is a new book: goal, items and receipts move together (R1-5b).

    Receipts are keyed by step id, and ids restart at 1 for every new list —
    so keeping the old plan's receipts silently swallows the new plan's
    completions (measured: backfill produced zero receipts).
    """
    from ai_coding.domain.plan_delivery import StepDelivery

    s = SessionData("s1", "title", "c", "l")
    s.task = TaskState(goal="old goal")
    s.task.add("old one")
    s.step_deliveries.append(
        StepDelivery(step_id="1", statement="old one", status="completed"))

    task = s.begin_plan("new goal")

    assert task.goal == "new goal" and task.items == []
    assert s.task is task
    assert s.step_deliveries == []


def test_begin_plan_keeps_messages_and_acceptance():
    """Only the plan moves; the conversation and its contracts stay."""
    from ai_coding.domain.acceptance import AcceptanceItem

    s = SessionData("s1", "title", "c", "l")
    s.add_message(_user("hi"))
    s.acceptance = [AcceptanceItem(id="AC-1", statement="x", verify="true")]
    s.task = TaskState(goal="old")

    s.begin_plan("new")

    assert len(s.messages) == 1
    assert [a.id for a in s.acceptance] == ["AC-1"]


# ---- P1/R1+R2: message id and the three durable recovery fields ---------------


def test_session_message_id_roundtrip():
    s = SessionData("s1", "title", "c", "l")
    s.add_message(_user("hi"))
    mid = s.messages[0].id
    assert mid
    restored = SessionData.from_dict(s.to_dict())
    assert restored.messages[0].id == mid


def test_session_from_dict_backfills_missing_message_id():
    java_json = {
        "sessionId": "abc",
        "title": "",
        "createdTime": "t0",
        "lastAccessTime": "t0",
        "messages": [{"role": "user", "content": "hi", "timestamp": "t0"}],
    }
    s = SessionData.from_dict(java_json)
    assert s.messages[0].id


def test_session_persists_new_fields():
    from ai_coding.domain.session import SessionData as SD

    s = SD("s1", "title", "c", "l")
    s.open_turn = 4
    s.undo_state = {"removed": [{"role": "user"}], "turnCount": 3, "digests": []}

    d = s.to_dict()
    assert d["openTurn"] == 4
    assert d["undoState"]["turnCount"] == 3

    # P3: the pending approval is marker-backed, never in the session JSON --
    # a mid-batch session save would run ``ensure_tool_pairing`` over the
    # still-unpaired call and drop it.
    s.pending_approval = {"callId": "c1", "toolName": "bash"}
    assert "pendingApproval" not in s.to_dict()

    restored = SD.from_dict(d)
    assert restored.open_turn == 4
    assert restored.undo_state["removed"] == [{"role": "user"}]
    assert restored.pending_approval is None  # only ``load`` fills it


def test_session_new_fields_absent_when_none():
    s = SessionData("s1", "title", "c", "l")
    d = s.to_dict()
    assert "openTurn" not in d
    assert "pendingApproval" not in d
    assert "undoState" not in d
    restored = SessionData.from_dict(d)
    assert restored.open_turn is None
    assert restored.pending_approval is None
    assert restored.undo_state is None


def test_session_recovered_interrupted_not_persisted():
    s = SessionData("s1", "title", "c", "l")
    s.recovered_interrupted = True
    assert "recoveredInterrupted" not in s.to_dict()


def test_open_turn_rejects_bool():
    """L2: JSON true is an int instance in Python, but it is not a turn."""
    data = {
        "sessionId": "s",
        "title": "",
        "createdTime": "",
        "lastAccessTime": "",
        "messages": [],
        "openTurn": True,
    }
    assert SessionData.from_dict(data).open_turn is None


def test_turn_usage_round_trips_the_context_reading():
    from ai_coding.domain.session import TurnUsage

    row = TurnUsage(
        turn=3,
        prompt_tokens=350,
        completion_tokens=12,
        total_tokens=362,
        context_tokens=250,
    )
    data = row.to_dict()
    assert data["contextTokens"] == 250
    assert TurnUsage.from_dict(data) == row
    # Old rows (no key) read as 0; the legacy fallback lives in the session rule.
    del data["contextTokens"]
    assert TurnUsage.from_dict(data).context_tokens == 0
