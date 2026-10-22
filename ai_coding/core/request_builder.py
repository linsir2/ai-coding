"""Assembling one request: the volatile tail and the tool context (R5).

The tail is the small region of a request that *is allowed* to change between
iterations — the memory constraint block, the task-state block, and whatever a
``turn.start`` hook injected.  Everything before it (the system prompt and the
history) stays byte-identical, which is what keeps the provider's prefix cache
alive (docs/r3-analysis.md 1.13).

Because that is a property worth protecting, the tail gets a name, a declared
order and its own tests instead of being thirty inline lines inside an already
229-line method.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext, TurnConclusion

if TYPE_CHECKING:
    from ai_coding.security.permissions import Rule

__all__ = ["TailBlock", "build_tail", "content_of", "tool_context", "volatile_block"]


@dataclass(frozen=True)
class TailBlock:
    """One volatile block of a request, with the label it is reported under."""

    name: str  # "memory" | "task" | "hook" | "digest"
    message: ChatMessage

    @property
    def chars(self) -> int:
        return len(self.message.content or "")


def volatile_block(name: str, text: str | None) -> TailBlock | None:
    """A tail block, or ``None`` when there is nothing to inject.

    An empty block is not an empty block: it is a message the model would pay for
    on every iteration, so "nothing to say" must mean "not in the request" (R7a;
    moved here from the loop, where it was ``_volatile``).
    """
    if not text:
        return None
    return TailBlock(
        name=name, message=ChatMessage(role="user", content=text, timestamp="")
    )


def content_of(message: ChatMessage | None) -> str | None:
    """The text of an optional injected message (``None`` when there is none)."""
    return message.content if message is not None else None


def build_tail(
    blocks: Sequence[TailBlock | None],
) -> tuple[list[ChatMessage], tuple[tuple[str, int], ...]]:
    """Turn tail blocks into ``(messages, sizes)``.

    Order is the caller's declaration order — never sorted, never merged — so the
    request is a deterministic function of what the loop decided to include, and
    ``ContextAssembled.tail_blocks`` can be read against the message list.
    """
    messages: list[ChatMessage] = []
    sizes: list[tuple[str, int]] = []
    for block in blocks:
        if block is None:
            continue
        messages.append(block.message)
        sizes.append((block.name, block.chars))
    return messages, tuple(sizes)


def tool_context(
    *,
    task: TaskState,
    workspace: str,
    session: SessionData,
    history: Sequence[ChatMessage],
    instructions: str | None,
    emit: Callable[[Any], None],
    turn: int = 0,
    note_checkpoint: Callable[[str], None] | None = None,
    note_approval: Callable[[dict[str, Any] | None], None] | None = None,
    note_plan_approved: Callable[[], None] | None = None,
    conclusion: TurnConclusion | None = None,
    jobs: Any = None,
    extra_workspace_roots: Sequence[str] = (),
    permissions: Sequence[Rule] = (),
    agent_rules: Sequence[Rule] = (),
) -> ToolContext:
    """The per-invocation context a tool receives (the dispatcher stamps call ids).

    ``turn`` is the session's turn counter (R7e-1): a note the model writes records
    where it came from, and ``source_turn`` has meant "turn" since R6d.
    """
    return ToolContext(
        task=task,
        workspace=workspace,
        session_id=session.session_id,
        # W2/R02: the operator's declared roots travel with the context, so a
        # tool that judges reachability asks the same question the workspace
        # gate asks (and gets the same answer).
        extra_workspace_roots=tuple(extra_workspace_roots),
        messages=tuple(history),
        # The uncompacted transcript, for the tools that must read past the
        # summary (history_search).  ``history`` above is this request's view.
        session_messages=tuple(session.messages),
        instructions=instructions,
        # 18 §10.1: the switch rides the one shared factory.
        readonly=session.readonly,
        # R03: ... and so does the posture fact (present_plan's gate).
        mode=session.mode,
        # M1a: the session's own layer rides this request, so a rule added
        # between turns is in force on the next one -- and never in the
        # process-wide gate another session is using.
        permissions=tuple(permissions),
        # M1b: the agent's narrowing rides the same request, for the same
        # reason -- it is a per-session fact, current on every call.
        agent_rules=tuple(agent_rules),
        emit=emit,
        turn=int(turn),
        note_checkpoint=note_checkpoint,
        note_approval=note_approval,
        note_plan_approved=note_plan_approved,
        # N1: the turn's conclusion holder -- the two exits of a plan-mode
        # turn read it to stay mutually exclusive.
        conclusion=conclusion,
        # D-1: the workspace's background jobs.  Present only where the product
        # builds one; `None` elsewhere and the tools that need it say so.
        jobs=jobs,
    )
