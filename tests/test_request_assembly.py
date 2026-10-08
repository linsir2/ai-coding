"""R7a: assembling one iteration's request — the four volatile blocks, in order.

The loop used to inline this: compress, build instructions, inject the task, ask
memory for its block, render the digest, build the tail, report the context.  R7a
moves the *what* (this unit) away from the *how* (``request_builder``), so the
loop body only names the step (docs/r7-analysis.md 3.1).
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import TurnDigest

pytestmark = pytest.mark.asyncio


class _Compactor:
    """Rewrites the history (or hands back the very same list)."""

    def __init__(self, rewrite: bool) -> None:
        self.rewrite = rewrite
        self.summaries: list[str] = []

    async def compress(self, history, *, deadline, on_summary=None, force=False, pinned=()):
        if not self.rewrite:
            return history
        return [*history, ChatMessage(role="user", content="[压缩后的历史]", timestamp="")]


class _Memory:
    def __init__(self, block: str = "") -> None:
        self.block_text = block

    def block(self) -> tuple[str, list[str]]:
        return self.block_text, (["n1"] if self.block_text else [])


class _Reporter:
    def __init__(self, tokens_spent: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._tokens_spent = tokens_spent

    def report_context(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"canonical-{len(self.calls)}"

    def tokens_spent(self) -> int:
        """The assembler reads this for the budget pressure block (G8-1); the
        real ``TurnReporter`` has had it since R4b."""
        return self._tokens_spent


class _Settings:
    working_memory_chars = 1200


def _session(digests: list[TurnDigest] | None = None) -> SessionData:
    session = SessionData(session_id="s1", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content="开始", timestamp="t"))
    session.turn_digests.extend(digests or [])
    return session


def _digest(turn: int = 1) -> TurnDigest:
    return TurnDigest(turn=turn, request=f"第 {turn} 步", outcome="完成了", status="completed")


def _assembler(
    *,
    rewrite: bool = True,
    memory_block: str = "",
    task_injection: str = "message",
    instructions: str | None = None,
) -> tuple[RequestAssembler, dict[str, Any]]:
    reporter = _Reporter()
    parts: dict[str, Any] = {
        "compactor": _Compactor(rewrite),
        "memory": _Memory(memory_block),
        "reporter": reporter,
        "events": [],
    }
    async def _instructions(deadline: Any) -> str | None:
        return instructions

    assembler = RequestAssembler(
        compactor=parts["compactor"],
        policy=TurnPolicy(TurnPolicyConfig(task_injection=task_injection)),
        memory=parts["memory"],
        reporter=reporter,
        build_instructions=_instructions,
        settings=_Settings(),
        emit=parts["events"].append,
    )
    return assembler, parts


async def _assemble(assembler: RequestAssembler, session: SessionData | None = None, **kwargs: Any):
    session = session or _session([_digest()])
    defaults: dict[str, Any] = {
        "session": session,
        "task": TaskState(goal="长任务"),
        "books": TurnBooks(),
        "turn_number": 2,
        "hook_context": "",
        "deadline": None,
        "index": 1,
        "previous_request": None,
    }
    return await assembler.assemble(**{**defaults, **kwargs})


# ------------------------------------------------------------------ the tail


async def test_blocks_keep_the_declared_order():
    assembler, _parts = _assembler(memory_block="<memory-constraints>x</memory-constraints>")
    session = _session([_digest()])
    assembled = await _assemble(
        assembler, session=session, hook_context="<hook-context>h</hook-context>"
    )
    contents = [message.content for message in assembled.request]
    assert "<memory-constraints>" in contents[-4]
    assert "task-state" in contents[-3]
    assert "<hook-context>" in contents[-2]
    assert "<working-memory>" in contents[-1]


async def test_an_empty_block_is_not_a_message():
    assembler, _parts = _assembler()
    assembled = await _assemble(assembler, session=_session([]))  # no digests → no ring
    # the history (one message, plus the rewrite the fake compactor made) and the
    # task block — memory, hook and digest each contributed nothing
    assert [message.role for message in assembled.request] == ["user", "user", "user"]
    assert "task-state" in assembled.request[-1].content
    assert "working-memory" not in assembled.request[-1].content


async def test_the_digest_block_only_appears_when_the_history_was_rewritten():
    kept, _ = _assembler(rewrite=False)
    rewritten, _ = _assembler(rewrite=True)
    session = _session([_digest()])
    assert "working-memory" not in (await _assemble(kept, session=session)).request[-1].content
    assert "working-memory" in (await _assemble(rewritten, session=session)).request[-1].content


async def test_the_memory_block_is_announced_once_per_turn():
    assembler, parts = _assembler(memory_block="<memory-constraints>x</memory-constraints>")
    books = TurnBooks()
    session = _session([_digest()])
    await _assemble(assembler, session=session, books=books, index=1)
    await _assemble(assembler, session=session, books=books, index=2)
    from ai_coding.core.events import MemoryInjected

    assert (
        len([e for e in parts["events"] if isinstance(e, MemoryInjected)]) == 1
    )  # one MemoryInjected for the whole turn
    assert books.memory_announced is True


# ------------------------------------------------------------- the contract


async def test_the_canonical_form_is_handed_back_for_the_next_iteration():
    assembler, parts = _assembler()
    first = await _assemble(assembler, previous_request=None)
    second = await _assemble(assembler, previous_request=first.previous_request)
    assert first.previous_request == "canonical-1"
    assert second.previous_request == "canonical-2"
    assert parts["reporter"].calls[1]["previous"] == "canonical-1"


async def test_the_task_block_can_be_folded_into_the_instructions():
    assembler, parts = _assembler(task_injection="system", instructions="你是助手。")
    assembled = await _assemble(assembler)
    assert assembled.instructions is not None
    assert "你是助手。" in assembled.instructions
    assert "task-state" in assembled.instructions
    assert parts["reporter"].calls[0]["instructions"] == assembled.instructions


async def test_the_history_that_was_compressed_is_what_the_engine_sees():
    assembler, parts = _assembler(rewrite=True)
    assembled = await _assemble(assembler)
    assert assembled.history[-1].content == "[压缩后的历史]"
    assert parts["reporter"].calls[0]["history"] is assembled.history


# ---- G8-1: the budget pressure block reaches the request --------------------


@pytest.mark.asyncio
async def test_a_nearly_exhausted_turn_gets_a_pressure_block():
    """The model can see none of the six budgets; the assembler is where the
    honest one gets a chance to speak before the cap is reached."""
    assembler, _parts = _assembler()
    assembler._policy = TurnPolicy(  # noqa: SLF001 - the config under test
        TurnPolicyConfig(max_iterations=10)
    )

    assembled = await _assemble(assembler, index=9)

    budget = [m for m in assembled.request if (m.content or "").startswith("[budget]")]
    assert len(budget) == 1
    assert "9/10" in budget[0].content


@pytest.mark.asyncio
async def test_a_healthy_turn_gets_no_pressure_block():
    """Heavy-handed pressure on every request would teach the model to ignore
    it, and would spend tail tokens on every iteration to say nothing."""
    assembler, _parts = _assembler()

    assembled = await _assemble(assembler, index=2)

    assert not [m for m in assembled.request if (m.content or "").startswith("[budget]")]


@pytest.mark.asyncio
async def test_a_compacted_history_reports_itself_exactly_once():
    assembler, parts = _assembler(rewrite=True)
    session = _session([_digest()])
    books = TurnBooks()

    await _assemble(assembler, session=session, books=books)
    await _assemble(assembler, session=session, books=books)

    from ai_coding.core.events import ContextCompacted

    assert len([e for e in parts["events"] if isinstance(e, ContextCompacted)]) == 1
