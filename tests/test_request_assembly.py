"""R7a: assembling one iteration's request — the four volatile blocks, in order.

The loop used to inline this: compress, build instructions, inject the task, ask
memory for its block, render the digest, build the tail, report the context.  R7a
moves the *what* (this unit) away from the *how* (``request_builder``), so the
loop body only names the step (docs/r7-analysis.md 3.1).
"""

from __future__ import annotations

import re
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
        self.compress_calls = 0

    async def compress(self, history, *, deadline, on_summary=None, force=False, pinned=()):
        self.compress_calls += 1
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

    def __init__(self, auto_compact: bool = True) -> None:
        self.auto_compact = auto_compact


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
    catalog: str = "",
    auto_compact: bool = True,
    workspace: str = "",
) -> tuple[RequestAssembler, dict[str, Any]]:
    reporter = _Reporter()
    parts: dict[str, Any] = {
        "compactor": _Compactor(rewrite),
        "memory": _Memory(memory_block),
        "reporter": reporter,
        "events": [],
    }
    async def _instructions(deadline: Any, session: Any = None) -> str | None:
        return instructions

    def _catalog() -> str:
        return catalog

    assembler = RequestAssembler(
        compactor=parts["compactor"],
        policy=TurnPolicy(TurnPolicyConfig(task_injection=task_injection)),
        memory=parts["memory"],
        reporter=reporter,
        build_instructions=_instructions,
        settings=_Settings(auto_compact=auto_compact),
        emit=parts["events"].append,
        catalog_builder=_catalog,
        workspace=workspace,
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


async def test_auto_compact_off_skips_the_proactive_compaction():
    """OC's settings.auto: with the switch off the budget-driven path never
    reaches the compactor (no call, no events); manual /compact and the
    overflow rescue run outside this gate and are unaffected."""
    off_assembler, off_parts = _assembler(auto_compact=False)
    await _assemble(off_assembler)
    assert off_parts["compactor"].compress_calls == 0

    on_assembler, on_parts = _assembler(auto_compact=True)
    await _assemble(on_assembler)
    assert on_parts["compactor"].compress_calls == 1


async def test_blocks_keep_the_declared_order():
    assembler, _parts = _assembler(memory_block="<memory-constraints>x</memory-constraints>")
    session = _session([_digest()])
    assembled = await _assemble(
        assembler, session=session, hook_context="<hook-context>h</hook-context>"
    )
    contents = [message.content for message in assembled.request]
    # R22/H11 adds an `anchor` block after every compaction, so the positions
    # shift; what the test is about is the *order*, not the offsets.
    order = [
        next(i for i, body in enumerate(contents) if needle in body)
        for needle in ("<memory-constraints>", "task-state", "<hook-context>", "<working-memory>")
    ]
    assert order == sorted(order), order


# ------------------------------------------------------- environment (13 §2.4)

#: What C-4 promises the model: one line carrying the moment *with its offset*,
#: so "本地时间" is a fact rather than an assumption (infra/time_utils.now_local).
_ISO_WITH_OFFSET = re.compile(r"时间: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


async def test_the_environment_block_leads_the_tail():
    assembler, parts = _assembler(workspace="/tmp/ws")
    session = _session([_digest()])
    session.git_branch = "feature/x"

    assembled = await _assemble(assembler, session=session)

    env = next(
        m.content or ""
        for m in assembled.request
        if (m.content or "").startswith("<environment>")
    )
    assert "工作区: /tmp/ws" in env
    assert "分支: feature/x" in env
    assert _ISO_WITH_OFFSET.search(env), "the block carries the moment, not just the day"
    # The moment includes the day; a second date line would be the same fact twice.
    assert "日期:" not in env
    names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    assert names[0] == "environment"


async def test_no_branch_line_when_git_says_nothing():
    assembler, _parts = _assembler(workspace="/tmp/ws")

    assembled = await _assemble(assembler, session=_session([_digest()]))

    env = next(
        m.content or ""
        for m in assembled.request
        if (m.content or "").startswith("<environment>")
    )
    assert "工作区: /tmp/ws" in env
    assert "分支:" not in env
    assert _ISO_WITH_OFFSET.search(env)


async def test_an_empty_block_is_not_a_message():
    assembler, _parts = _assembler()
    assembled = await _assemble(assembler, session=_session([]))  # no digests → no ring
    # the history (one message, plus the rewrite the fake compactor made), the
    # environment block and the task block — memory, hook and digest each
    # contributed nothing (13 §2.4: environment is never empty).
    # R22/H11: the compaction this fake compactor performed also earns the
    # `anchor` caveat (the model is working from a shortened history), so the
    # tail is four blocks rather than three.
    assert [message.role for message in assembled.request] == [
        "user", "user", "user", "user", "user",
    ]
    # Declared order: environment → task → anchor.  The caveat closes the tail.
    assert "<environment>" in assembled.request[-3].content
    assert "task-state" in assembled.request[-2].content
    assert "重新核算" in assembled.request[-1].content
    assert "working-memory" not in "".join(m.content or "" for m in assembled.request)


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
async def test_a_plan_turn_gets_no_pressure_block():
    """P0b-fix-plan-budget: a plan turn has no step ceiling, so a step-share
    warning would describe a cap it does not have."""
    assembler, _parts = _assembler()
    assembler._policy = TurnPolicy(  # noqa: SLF001 - the config under test
        TurnPolicyConfig(max_iterations=10)
    )
    session = _session()
    session.agent = "plan"

    assembled = await _assemble(assembler, session=session, index=9)

    assert not [m for m in assembled.request if (m.content or "").startswith("[budget]")]


@pytest.mark.asyncio
async def test_a_plan_implementing_turn_gets_no_pressure_block_late_in_the_turn():
    """C-3: execution runs with ``mode == "normal"`` on the codex path, so the
    global step cap used to inject "步数 12/15" at step 12 and make the model
    wrap up early.  Plan execution has no step/token budget."""
    assembler, parts = _assembler()
    assembler._policy = TurnPolicy(  # noqa: SLF001 - the config under test
        TurnPolicyConfig(max_iterations=15)
    )
    session = _session()
    session.plan_implementing = True

    assembled = await _assemble(assembler, session=session, index=12)

    budget = [m for m in assembled.request if (m.content or "").startswith("[budget]")]
    assert not budget
    names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    assert "budget" not in names


@pytest.mark.asyncio
async def test_an_ordinary_turn_still_gets_pressure_at_the_same_step():
    """The plan-execution exemption must not leak into ordinary turns."""
    assembler, parts = _assembler()
    assembler._policy = TurnPolicy(  # noqa: SLF001 - the config under test
        TurnPolicyConfig(max_iterations=15)
    )

    assembled = await _assemble(assembler, index=12)

    budget = [m for m in assembled.request if (m.content or "").startswith("[budget]")]
    assert len(budget) == 1
    assert "12/15" in budget[0].content
    names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    assert "budget" in names


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


async def test_the_mcp_catalog_rides_the_volatile_tail():
    """M2: the catalog is per-step state, not a frozen schema — it must land in
    the tail (the only region allowed to differ between iterations) so a server
    that reconnects with new tools is visible on the very next step."""
    assembler, parts = _assembler(catalog="fs\n  read_file: reads")
    assembled = await _assemble(assembler)

    texts = [m.content for m in assembled.request]
    assert any("read_file" in (t or "") for t in texts)
    # And its size is handed to the reporter with every other block's, which is
    # what ends up in ContextAssembled — so the trace shows what the catalog cost.
    block = ("mcp_catalog", len("fs\n  read_file: reads"))
    assert block in parts["reporter"].calls[-1]["tail_blocks"]


# ---- D-7: a recall hint, only where there is something to recall -------------


def _env_of(assembled) -> str:
    return next(
        m.content or ""
        for m in assembled.request
        if (m.content or "").startswith("<environment>")
    )


async def test_no_recall_hint_in_a_session_that_was_never_compacted():
    """The hint costs bytes every iteration; a session whose history was never
    rewritten has nothing to recall, so it pays nothing."""
    assembler, _parts = _assembler(workspace="/tmp/ws")

    assembled = await _assemble(assembler, session=_session([_digest()]))

    assert "history_search" not in _env_of(assembled)


async def test_a_compacted_session_is_told_where_its_words_went():
    """D-7B: after a compaction replaced the transcript with a summary, the
    model is told -- once per request, in the volatile tail -- that exact
    wording lives behind history_search. The condition is last_compacted_turn,
    which the funnel already maintains; no new state is invented for this."""
    assembler, _parts = _assembler(workspace="/tmp/ws")
    session = _session([_digest()])
    session.last_compacted_turn = 3

    assembled = await _assemble(assembler, session=session)

    env = _env_of(assembled)
    assert "history_search" in env
    assert "压缩" in env, "the line must say what happened, not just name a tool"


# ---- R01: the policy in force is the live one, not the build-time snapshot --


@pytest.mark.asyncio
async def test_the_budget_block_reads_the_provider_not_the_construction_snapshot():
    """R01 (F-51): the planning round and every plan step swap ``loop.policy``
    for their own ceiling.  The assembler used to keep reading the object it
    was built with, so ``[budget]`` described a budget the turn no longer had
    — the provider is how the live loop hands it the current one."""
    reporter = _Reporter()

    async def _instructions(deadline: Any, session: Any = None) -> str | None:
        return None

    assembler = RequestAssembler(
        compactor=_Compactor(True),
        # The construction snapshot says "plenty of room" (100 rounds) ...
        policy=TurnPolicy(TurnPolicyConfig(max_iterations=100)),
        # ... while the policy actually in force is down to its last round.
        policy_provider=lambda: TurnPolicy(TurnPolicyConfig(max_iterations=10)),
        memory=_Memory(""),
        reporter=reporter,
        build_instructions=_instructions,
        settings=_Settings(),
        emit=lambda event: None,
    )

    assembled = await _assemble(assembler, index=9)

    budget = [m for m in assembled.request if (m.content or "").startswith("[budget]")]
    assert len(budget) == 1
    assert "9/10" in budget[0].content


async def test_an_approved_plan_missing_from_history_gets_a_compact_plan_record():
    assembler, parts = _assembler()
    session = _session([_digest()])
    session.approved_plan = "## Plan\n1. build frontend"

    assembled = await _assemble(assembler, session=session)

    bodies = "\n".join(message.content or "" for message in assembled.request)
    assert "<plan-record>" in bodies
    assert "<plan-state>" not in bodies
    assert "## Plan\n1. build frontend" not in bodies
    names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    assert "plan_record" in names
    assert "plan_state" not in names


async def test_plan_implementing_keeps_the_task_state_and_drops_the_plan_state():
    assembler, _parts = _assembler()
    session = _session([_digest()])
    session.agent = "build"
    session.plan_implementing = True
    session.approved_plan = "## Plan\n1. build frontend"
    task = TaskState(goal="Implement the plan.")
    task.add("Step 6: RoomPage")

    assembled = await _assemble(assembler, session=session, task=task)

    bodies = "\n".join(message.content or "" for message in assembled.request)
    assert "<plan-state>" not in bodies
    assert "<task-state>" in bodies
    assert "Step 6: RoomPage" in bodies


async def test_a_plan_already_in_history_is_not_repeated_in_the_tail():
    """The approved plan is already an assistant message; the tail must not
    carry a second full copy every iteration."""
    assembler, parts = _assembler()
    session = _session([_digest()])
    plan = "## Plan\n1. build frontend\n\n" + ("detail " * 300)
    session.approved_plan = plan
    session.messages.append(
        ChatMessage(
            role="assistant",
            content=f"here is it\n<proposed_plan>{plan}</proposed_plan>",
            timestamp="t",
        )
    )

    assembled = await _assemble(assembler, session=session)

    tail_names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    tail_text = "\n".join(
        message.content or "" for message in assembled.request[-len(tail_names):]
    )
    assert "plan_state" not in tail_names
    assert "<plan-state>" not in tail_text
    assert "<plan-record>" not in tail_text
    assert plan not in tail_text


async def test_a_plan_found_in_any_history_role_is_not_repeated():
    assembler, parts = _assembler()
    session = _session([_digest()])
    plan = "## Plan\n1. build frontend"
    session.approved_plan = plan
    session.messages.append(ChatMessage(role="user", content=plan, timestamp="t"))

    await _assemble(assembler, session=session)

    tail_names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    assert "plan_record" not in tail_names


async def test_a_missing_plan_body_gets_only_a_compact_plan_record():
    assembler, parts = _assembler()
    session = _session([_digest()])
    plan = "## Frontend plan\n1. build frontend\n\n" + ("detail " * 300)
    session.approved_plan = plan

    assembled = await _assemble(assembler, session=session)

    tail_names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    tail_text = "\n".join(
        message.content or "" for message in assembled.request[-len(tail_names):]
    )
    assert "plan_record" in tail_names
    assert "<plan-record>" in tail_text
    assert "Frontend plan" in tail_text
    assert plan not in tail_text
    assert "history_search" in tail_text


async def test_plan_implementing_shows_the_task_and_not_a_full_plan_tail():
    assembler, parts = _assembler()
    session = _session([_digest()])
    session.agent = "build"
    session.plan_implementing = True
    plan = "## Plan\n1. build frontend"
    session.approved_plan = plan
    task = TaskState(goal="Implement the plan.")
    task.add("Step 6: RoomPage")

    assembled = await _assemble(assembler, session=session, task=task)

    tail_names = [name for name, _size in parts["reporter"].calls[-1]["tail_blocks"]]
    tail_text = "\n".join(
        message.content or "" for message in assembled.request[-len(tail_names):]
    )
    assert "task" in tail_names
    assert "Step 6: RoomPage" in tail_text
    assert "<plan-state>" not in tail_text
