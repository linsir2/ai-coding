"""Session persistence contract, compatible with the Java ``sessions/*.json`` shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .message import ChatMessage, ToolCallRef, tool_call_from_ref
from .task import TaskState
from .turn_digest import TurnDigest


def _message_from_dict(data: dict[str, Any]) -> ChatMessage:
    """Rebuild a ChatMessage from a persisted JSON dict (Java-compatible shape)."""
    tool_calls = None
    raw_calls = data.get("toolCalls")
    if isinstance(raw_calls, list):
        tool_calls = [
            ToolCallRef(
                id=item["id"],
                name=item["name"],
                arguments=item.get("arguments", ""),
            )
            for item in raw_calls
            if isinstance(item, dict) and item.get("id") and item.get("name")
        ]
    return ChatMessage(
        role=data["role"],
        content=data.get("content"),
        timestamp=data.get("timestamp", ""),
        tool_call_id=data.get("toolCallId"),
        tool_name=data.get("toolName"),
        tool_calls=tool_calls,
    )


def _message_to_dict(msg: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
        "timestamp": msg.timestamp,
    }
    if msg.tool_call_id is not None:
        data["toolCallId"] = msg.tool_call_id
    if msg.tool_name is not None:
        data["toolName"] = msg.tool_name
    if msg.tool_calls:
        data["toolCalls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in msg.tool_calls
        ]
    return data


@dataclass
class SessionData:
    """A persisted conversation session."""

    session_id: str
    title: str
    created_time: str
    last_access_time: str
    messages: list[ChatMessage] = field(default_factory=list)
    task: TaskState | None = None
    # R3b: how many messages have already been harvested into memory.  The cursor
    # lives in the session (not in the loop) because the CLI builds a fresh loop
    # for every turn, while the session is reloaded each time.
    harvested_messages: int = 0
    # R6a: the session-level turn counter (1-based) and the bounded ring of
    # one-line turn digests the volatile <working-memory> block is rendered from.
    # The counter exists so `source_turn` and the digest header can say "turn N"
    # instead of "message count" (docs/r6-analysis.md 2.4).
    turn_count: int = 0
    turn_digests: list[TurnDigest] = field(default_factory=list)
    # NOT persisted — ``to_dict`` is a hand-written whitelist, so this stays out
    # of the on-disk format and the Java side keeps reading the same bytes.
    # ``SessionService.list_sessions`` reads every file anyway and drops the
    # bodies; without this the only count a list view could show was the zero it
    # just wrote itself.  -1 means "not computed" (a full ``load``), so callers
    # must treat it as "use ``len(messages)``" rather than as zero.
    message_count: int = -1
    # NOT persisted — ``to_dict`` is a hand-written whitelist, so this stays out
    # of the on-disk format.  It is a normalized excerpt from the first user
    # message, used by the resume picker.
    first_preview: str = ""

    def add_message(self, message: ChatMessage) -> None:
        self.messages.append(message)

    def ensure_tool_pairing(self, strict: bool = True) -> None:
        """Drop orphan tool results and strip unmatched tool calls (defensive)."""
        declared = {
            c.id
            for m in self.messages
            if m.role == "assistant" and m.tool_calls
            for c in m.tool_calls
        }
        result_ids = {
            m.tool_call_id
            for m in self.messages
            if m.role == "tool" and m.tool_call_id in declared
        }
        keep: list[ChatMessage] = []
        for m in self.messages:
            if m.role == "tool":
                if m.tool_call_id in declared:
                    keep.append(m)
                continue
            if strict and m.role == "assistant" and m.tool_calls and not m.content:
                # Only strip orphan calls from "carrier" messages (no text content).
                # For messages with text, keep all tool_calls as historical record.
                kept = [c for c in m.tool_calls if c.id in result_ids]
                if not kept:
                    continue  # assistant carrier message with no surviving calls
                m.tool_calls = kept
            keep.append(m)
        self.messages = keep

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sessionId": self.session_id,
            "title": self.title,
            "createdTime": self.created_time,
            "lastAccessTime": self.last_access_time,
            "messages": [_message_to_dict(m) for m in self.messages],
        }
        # Only persist the task list when it actually holds items: a bare goal is
        # already recoverable from the first user message, so writing it would be
        # noise in every session file.
        if self.task is not None and self.task.items:
            data["task"] = self.task.to_dict()
        if self.harvested_messages:
            data["harvestedMessages"] = self.harvested_messages
        # Same rule as `task`: an empty ring is not worth a key in every file.
        if self.turn_count:
            data["turnCount"] = int(self.turn_count)
        if self.turn_digests:
            data["turnDigests"] = [digest.to_dict() for digest in self.turn_digests]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionData:
        messages = [
            _message_from_dict(m)
            for m in data.get("messages", [])
            if isinstance(m, dict)
        ]
        raw_task = data.get("task")
        raw_digests = data.get("turnDigests")
        rows = raw_digests if isinstance(raw_digests, list) else []
        digests = [
            digest
            for digest in (TurnDigest.from_dict(row) for row in rows)
            if digest is not None
        ]
        return cls(
            session_id=data.get("sessionId", ""),
            title=data.get("title", ""),
            created_time=data.get("createdTime", ""),
            last_access_time=data.get("lastAccessTime", ""),
            messages=messages,
            task=TaskState.from_dict(raw_task) if isinstance(raw_task, dict) else None,
            harvested_messages=int(data.get("harvestedMessages", 0) or 0),
            turn_count=int(data.get("turnCount", 0) or 0),
            turn_digests=digests,
        )


# Re-export helper for callers that need to resolve references.
__all__ = ["SessionData", "tool_call_from_ref"]
