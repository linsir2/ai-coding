"""Assembling one iteration's request (R7a).

`request_builder` owns the *shape* of a request (the tail is a declared, ordered
list of volatile blocks); this unit owns the *content* of one: it compresses the
history, builds the static instructions, folds the task list in where the policy
wants it, and asks memory and the digest ring for their blocks.

That is worth its own name because two rules live here and nowhere else:

* **order is a contract** — memory / task / hook / digest, never sorted, never
  merged, so ``ContextAssembled.tail_blocks`` can be read against the messages;
* the digest ring and the memory block are **conditional** — the ring only pays
  for itself when this request's history was actually rewritten (R6a), and the
  memory block is announced once per turn, not once per iteration (R3).

R7a moved this out of ``process_input``, which had grown to 228 lines because
every new block was another ten lines inside the same method.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ai_coding.core.compaction import Compactor
from ai_coding.core.events import LoopEvent, MemoryInjected
from ai_coding.core.request_builder import build_tail, content_of, volatile_block
from ai_coding.core.turn_books import TurnBooks
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import render_working_memory
from ai_coding.infra.deadline import Deadline

__all__ = ["Assembled", "RequestAssembler"]


@dataclass(frozen=True)
class Assembled:
    """One iteration's request, ready for the engine."""

    history: list[ChatMessage]
    instructions: str | None
    request: list[ChatMessage]
    #: The canonical form of this request, which the next iteration compares against.
    previous_request: str


class RequestAssembler:
    """Build one request per iteration, from the turn's own state."""

    def __init__(
        self,
        *,
        compactor: Any,
        policy: Any,
        memory: Any,
        reporter: Any,
        build_instructions: Callable[[Deadline | None], Awaitable[str | None]] | None = None,
        settings: Any,
        emit: Callable[[LoopEvent], None],
    ) -> None:
        self._compactor = compactor
        self._policy = policy
        self._memory = memory
        self._reporter = reporter
        # The system prompt belongs to the composition root (project instructions +
        # the skills catalogue), so the loop hands its builder in rather than the
        # assembler learning about prompt assembly.
        self._build_instructions = build_instructions
        self._settings = settings
        self._emit = emit

    async def assemble(
        self,
        *,
        session: SessionData,
        task: TaskState,
        books: TurnBooks,
        turn_number: int,
        hook_context: str,
        deadline: Deadline | None,
        index: int,
        previous_request: str | None,
    ) -> Assembled:
        """Compress, instruct, inject, tail — in that order, every iteration."""
        history = await self._compactor.compress(
            session.messages, deadline=deadline, on_summary=books.summaries.append
        )
        # R6a: the ring only pays for itself when this request's history was
        # actually rewritten — otherwise the recent turns are still there
        # (docs/r6-analysis.md 2.1, D-R6-2).
        compacted = Compactor.did_compact(session.messages, history)
        instructions = (
            await self._build_instructions(deadline)
            if self._build_instructions is not None
            else None
        )
        injection = self._policy.inject_task(task, instructions)
        instructions = injection.instructions
        # Memory and task state are *volatile tail blocks*: the system prompt stays
        # byte-identical and the history stays append-only, so only this small
        # region diverges between turns (docs/r2 §1.13).
        memory_block, memory_ids = self._memory.block()
        if memory_block and not books.memory_announced:
            self._emit(MemoryInjected(note_ids=memory_ids, chars=len(memory_block)))
            books.memory_announced = True
        digest_block = render_working_memory(
            session.turn_digests,
            current_turn=turn_number,
            max_chars=int(getattr(self._settings, "working_memory_chars", 1200)),
            compacted=compacted,
        )
        # G8-1: the one block that tells the model its budget is nearly gone.
        # Last, after the digest, because it is an instruction and instructions
        # are the thing recency helps -- everything above it is state to read,
        # this is the one line asking for a change of behaviour.
        pressure = self._policy.budget_pressure(
            iteration=index, tokens_spent=self._reporter.tokens_spent()
        )
        tail, tail_blocks = build_tail(
            [
                volatile_block("memory", memory_block),
                volatile_block("task", content_of(injection.message)),
                volatile_block("hook", hook_context),
                volatile_block("digest", digest_block),
                volatile_block("budget", pressure),
            ]
        )
        request = [*history, *tail]
        canonical = self._reporter.report_context(
            iteration=index,
            instructions=instructions,
            history=history,
            request=request,
            tail_blocks=tail_blocks,
            previous=previous_request,
        )
        return Assembled(
            history=history,
            instructions=instructions,
            request=request,
            previous_request=canonical,
        )
