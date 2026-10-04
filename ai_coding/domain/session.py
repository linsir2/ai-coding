"""Session persistence contract, compatible with the Java ``sessions/*.json`` shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .acceptance import AcceptanceItem
from .message import ChatMessage, ToolCallRef, tool_call_from_ref
from .plan_delivery import StepDelivery
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
        reasoning=data.get("reasoning"),
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
    # R1-4: thinking travels with its step, written only when present — old
    # files (and the Java reader) never see the key.
    if msg.reasoning:
        data["reasoning"] = msg.reasoning
    return data


@dataclass(frozen=True)
class TurnUsage:
    """One turn's token spend, for post-hoc forensics (was memory-only).

    ``TurnOutcome.usage`` is filled by the reporter after the session save, so
    a past length-cutoff could never be confirmed from the file.  Rows are
    appended in turn order; turns that spent nothing (or reported nothing)
    leave no row rather than a row of zeros.
    """

    turn: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": int(self.turn),
            "promptTokens": int(self.prompt_tokens),
            "completionTokens": int(self.completion_tokens),
            "totalTokens": int(self.total_tokens),
        }

    @classmethod
    def from_dict(cls, data: Any) -> TurnUsage | None:
        if not isinstance(data, dict):
            return None
        try:
            turn = int(data.get("turn", 0) or 0)
        except (TypeError, ValueError):
            return None
        if turn <= 0:
            return None
        return cls(
            turn=turn,
            prompt_tokens=int(data.get("promptTokens", 0) or 0),
            completion_tokens=int(data.get("completionTokens", 0) or 0),
            total_tokens=int(data.get("totalTokens", 0) or 0),
        )


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
    # Per-turn token spend (see TurnUsage).  Sessions written before this
    # existed simply have no key; the file stays readable everywhere.
    turn_usages: list[TurnUsage] = field(default_factory=list)
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
    # Acceptance contracts (R0-1): machine-checkable statements plus the shell
    # command proving each one.  Populated programmatically for now (the plan
    # runner will produce these); persisted only when non-empty, same rule as
    # ``task`` — old files simply have no key.
    acceptance: list[AcceptanceItem] = field(default_factory=list)
    # C3: continuation rounds this turn has already spent (the iteration budget
    # died with items open, the loop started a fresh round).  Kept on the
    # session because the books are per-turn and would lose it across rounds —
    # in-process only: ``to_dict`` is a whitelist and a new user turn resets it.
    continuations: int = 0
    # NOT persisted, by the same whitelist.  Internal sessions (plan-runner
    # steps) never touch the sessions directory: they run on the main loop,
    # whose TurnCloser would otherwise write one "plan step N" file per step
    # and flood the /resume picker.
    ephemeral: bool = False
    # Step deliveries (R1-1): what each plan-runner step left behind.  The
    # resume replay reads these to rebuild the step timeline; persisted only
    # when non-empty, same rule as above.
    step_deliveries: list[StepDelivery] = field(default_factory=list)

    def begin_plan(self, goal: str) -> TaskState:
        """Start a new plan: fresh goal, no items, no step receipts (R1-5b).

        The three belong together.  Receipts are keyed by step id and ids
        restart at 1 for every new list, so carrying the old plan's receipts
        over made the runner's backfill believe the new plan's steps were
        already receipted (measured: a reset plan produced zero receipts).
        One owner for the trio, so ``/plan`` cannot reset the goal and
        forget the book again.
        """
        self.task = TaskState(goal=goal)
        self.step_deliveries = []
        return self.task

    def add_message(self, message: ChatMessage) -> None:
        self.messages.append(message)

    def record_turn_usage(self, turn: int, usage: dict[str, int] | None) -> None:
        """Append one usage row; absent or all-zero usage writes no row."""
        if not usage or not any(int(value or 0) for value in usage.values()):
            return
        self.turn_usages.append(
            TurnUsage(
                turn=int(turn),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
            )
        )

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
        # Same rule again: zero-spend turns leave no rows, so most test and
        # idle sessions never grow this key at all.
        if self.turn_usages:
            data["turnUsages"] = [row.to_dict() for row in self.turn_usages]
        # Same rule again: sessions without acceptance contracts write no key,
        # so every file written before R0-1 keeps reading byte-identically.
        if self.acceptance:
            data["acceptance"] = [item.to_dict() for item in self.acceptance]
        # Same rule again: sessions never plan-run write no key.
        if self.step_deliveries:
            data["stepDeliveries"] = [entry.to_dict() for entry in self.step_deliveries]
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
        raw_usages = data.get("turnUsages")
        usage_rows = raw_usages if isinstance(raw_usages, list) else []
        usages = [
            row
            for row in (TurnUsage.from_dict(item) for item in usage_rows)
            if row is not None
        ]
        raw_acceptance = data.get("acceptance")
        acceptance_rows = raw_acceptance if isinstance(raw_acceptance, list) else []
        acceptance = [
            AcceptanceItem.from_dict(row) for row in acceptance_rows
            if isinstance(row, dict)
        ]
        raw_deliveries = data.get("stepDeliveries")
        delivery_rows = raw_deliveries if isinstance(raw_deliveries, list) else []
        step_deliveries = [
            StepDelivery.from_dict(row) for row in delivery_rows
            if isinstance(row, dict)
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
            turn_usages=usages,
            acceptance=acceptance,
            step_deliveries=step_deliveries,
        )


# Re-export helper for callers that need to resolve references.
__all__ = ["SessionData", "TurnUsage", "tool_call_from_ref"]
