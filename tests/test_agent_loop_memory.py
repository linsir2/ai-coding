"""TDD (R3b): how memory meets the loop.

The two invariants from docs/r3-analysis.md 1.16, plus the data-flow rules:

  1. the system prompt is **byte-identical across turns** (nothing dynamic in it);
  2. memory may only appear as the volatile constraint block at the tail, or as a
     ``memory_search`` tool result;

and on the write side: harvest sees **the turn's delta**, receives the L4 summary as
an input, and never breaks a turn.
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.prompt import PromptAssembler
from ai_coding.config.models import AIConfig, MemoryConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.context_compression import ContextManager
from ai_coding.core.events import MemoryHarvested, MemoryInjected, RecordingObserver
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.memory.note import NoteKind, new_note
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Records every request; answers immediately."""

    def __init__(self, reply: str = "好的") -> None:
        self.reply = reply
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(
            {"messages": list(messages), "instructions": instructions or "", "tools": tools}
        )
        return StepResult(text=self.reply, finish_reason="stop")


class _Extractor:
    def __init__(self, *items) -> None:
        self.items = list(items)
        self.seen: list[list[ChatMessage]] = []

    async def __call__(self, messages, conclusion):
        self.seen.append(list(messages))
        return list(self.items)


def _cfg(**overrides) -> AIConfig:
    base = {"stop_policy": "natural", "max_tool_iterations": 1}
    base.update(overrides)
    return AIConfig(**base)


def _memory(tmp_path, extractor=None, **overrides) -> MemoryService:
    config = MemoryConfig(**overrides)
    return MemoryService(MemoryStore(tmp_path / "m.json"), extractor=extractor, config=config)


def _loop(
    engine,
    sessions,
    memory=None,
    *,
    settings=None,
    cfg=None,
    ws=None,
    observer=None,
    summarizer=None,
):
    workspace = ws or (sessions.root if hasattr(sessions, "root") else ".")
    context = (
        ContextManager(cfg, transcripts_root=workspace, summarizer=summarizer)
        if cfg is not None
        else None
    )
    return AgentLoop(
        engine,
        sessions,
        ToolRegistry(),
        context=context,
        memory=memory,
        prompt=PromptAssembler("You are a coding assistant."),
        settings=settings or _cfg(),
        observer=observer,
        workspace=str(workspace),
    )


@pytest.mark.asyncio
async def test_system_prompt_is_byte_identical_across_turns(tmp_path):
    """Invariant 1: nothing dynamic (and no memory) may enter the system prompt."""
    store = MemoryStore(tmp_path / "m.json")
    extractor = _Extractor("项目用 uv 管理依赖")
    memory = MemoryService(store, extractor=extractor, config=MemoryConfig())
    sessions = SessionService(tmp_path / "sessions")
    engine = _Engine()
    loop = _loop(engine, sessions, memory)
    session = sessions.create()

    await loop.process_input(session, "第一轮")
    await loop.process_input(session, "第二轮：项目用什么管依赖")
    await loop.flush_harvest()

    assert store.count >= 1  # memory really changed between the two turns
    assert engine.requests[0]["instructions"] == engine.requests[1]["instructions"]
    assert "<memory" not in engine.requests[1]["instructions"]


@pytest.mark.asyncio
async def test_constraint_block_is_injected_at_the_tail(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        new_note("安全红线：不得把密钥写进代码", kind=NoteKind.DECISION, pinned=True)
    )
    memory = MemoryService(store, config=MemoryConfig())
    sessions = SessionService(tmp_path / "sessions")
    engine = _Engine()
    loop = _loop(engine, sessions, memory)

    await loop.process_input(sessions.create(), "写点代码")

    messages = engine.requests[0]["messages"]
    block_index = next(
        index
        for index, message in enumerate(messages)
        if (message.content or "").startswith("<memory-constraints>")
    )
    assert block_index >= len(messages) - 3  # the tail, not the head
    assert "安全红线" in (messages[block_index].content or "")
    assert "<memory" not in engine.requests[0]["instructions"]


@pytest.mark.asyncio
async def test_recall_details_are_not_injected_automatically(tmp_path):
    """Details come from the tool; nothing gets stuffed into the prompt."""
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("部署流程：先跑 make build，产物在 dist/"))
    memory = MemoryService(store, config=MemoryConfig())
    sessions = SessionService(tmp_path / "sessions")
    engine = _Engine()
    loop = _loop(engine, sessions, memory)

    await loop.process_input(sessions.create(), "部署怎么做？")

    request = engine.requests[0]
    joined = request["instructions"] + "\n".join(
        message.content or "" for message in request["messages"]
    )
    assert "make build" not in joined


@pytest.mark.asyncio
async def test_harvest_sees_only_the_turn_delta(tmp_path):
    extractor = _Extractor("事实")
    memory = _memory(tmp_path, extractor)
    sessions = SessionService(tmp_path / "sessions")
    engine = _Engine()
    loop = _loop(engine, sessions, memory)
    session = sessions.create()

    await loop.process_input(session, "第一轮：记住用 uv")
    await loop.process_input(session, "第二轮：继续")
    await loop.process_input(session, "第三轮：再说一句")
    await loop.flush_harvest()

    batches = ["\n".join(m.content or "" for m in batch) for batch in extractor.seen]
    # A delta is "the messages added since the last harvest" — user *and* the
    # assistant's answer (its conclusion can carry the fact) — but never a
    # repeat.  Since the harvest is scheduled (2026-10-17) rapid turns may
    # coalesce into one delta; what must hold under any interleaving is that
    # every turn's words go to the extractor exactly once, in order.
    for keyword in ("第一轮：记住用 uv", "第二轮：继续", "第三轮：再说一句"):
        total = sum(keyword in batch for batch in batches)
        assert total == 1, f"{keyword} appeared {total} times across {len(batches)} batches"
    order = [
        next(i for i, batch in enumerate(batches) if keyword in batch)
        for keyword in ("第一轮：记住用 uv", "第二轮：继续", "第三轮：再说一句")
    ]
    assert order == sorted(order), order


@pytest.mark.asyncio
async def test_the_harvest_cursor_survives_a_new_loop_on_the_same_session(tmp_path):
    """The CLI builds a fresh loop per turn; the cursor lives in the session."""
    extractor = _Extractor("事实")
    memory = _memory(tmp_path, extractor)
    sessions = SessionService(tmp_path / "sessions")
    session = sessions.create()

    first_loop = _loop(_Engine(), sessions, memory)
    await first_loop.process_input(session, "第一轮")
    await first_loop.flush_harvest()
    reloaded = sessions.load(session.session_id)
    assert reloaded is not None and reloaded.harvested_messages > 0

    second_loop = _loop(_Engine(), sessions, memory)
    await second_loop.process_input(session, "第二轮")
    await second_loop.flush_harvest()

    second = [message.content for message in extractor.seen[1]]
    assert any("第二轮" in (content or "") for content in second)
    assert not any("第一轮" in (content or "") for content in second)


@pytest.mark.asyncio
async def test_harvest_stores_what_the_extractor_returns(tmp_path):
    extractor = _Extractor(
        HarvestCandidate(text="项目用 uv 管理依赖", kind=NoteKind.DECISION, importance=5)
    )
    memory = _memory(tmp_path, extractor)
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(_Engine(), sessions, memory)
    observer = RecordingObserver()
    loop.observer = observer

    await loop.process_input(sessions.create(), "记住：项目用 uv 管理依赖")
    await loop.flush_harvest()

    assert memory.store.count == 1
    assert memory.store.all()[0].kind is NoteKind.DECISION
    harvested = observer.of_type(MemoryHarvested)
    assert harvested and harvested[0].added


@pytest.mark.asyncio
async def test_injected_block_emits_an_event(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("项目用 uv 管理依赖", kind=NoteKind.DECISION))
    memory = MemoryService(store, config=MemoryConfig())
    sessions = SessionService(tmp_path / "sessions")
    observer = RecordingObserver()
    loop = _loop(_Engine(), sessions, memory, observer=observer)

    await loop.process_input(sessions.create(), "继续")

    injected = observer.of_type(MemoryInjected)
    assert injected and injected[0].chars > 0 and injected[0].note_ids


@pytest.mark.asyncio
async def test_l4_summary_reaches_the_harvest(tmp_path):
    extractor = _Extractor("事实")
    memory = _memory(tmp_path, extractor)
    sessions = SessionService(tmp_path / "sessions")
    session = sessions.create()

    async def summarizer(text: str) -> str:
        return "摘要：决定用 uv"

    cfg = _cfg(max_context_tokens=60, max_messages=400, max_tool_iterations=1)
    loop = _loop(
        _Engine("很长的一段回答" * 40),
        sessions,
        memory,
        settings=cfg,
        cfg=cfg,
        ws=tmp_path,
        summarizer=summarizer,
    )

    await loop.process_input(session, "第一轮" * 20)
    await loop.process_input(session, "第二轮" * 20)
    await loop.flush_harvest()

    summaries = [
        message.content
        for batch in extractor.seen
        for message in batch
        if message.role == "system"
    ]
    marker = "摘要：决定用 uv"
    assert any(marker in (content or "") for content in summaries)


@pytest.mark.asyncio
async def test_a_broken_memory_service_never_breaks_the_turn(tmp_path):
    class _Broken(MemoryService):
        async def recall_notes(self, *_args, **_kwargs):
            raise RuntimeError("recall down")

        def index_block(self, **_kwargs):
            raise RuntimeError("index down")

        async def harvest(self, *_args, **_kwargs):
            raise RuntimeError("harvest down")

    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(_Engine("answer"), sessions, _Broken(MemoryStore(tmp_path / "m.json")))

    text = await loop.process_input(sessions.create(), "go")
    await loop.flush_harvest()  # the broken harvest must not surface here

    assert text == "answer"
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.COMPLETED


# ---- R8a D-14: writing a checkpoint is not evidence about the world --------


class _CheckpointThenAnswer(ChatEngine):
    """Calls checkpoint_write once, then answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(
                        id="c1", name="checkpoint_write", arguments='{"note": "记一行"}'
                    )
                ],
                finish_reason="tool_calls",
            )
        return StepResult(text="好了", finish_reason="stop")


class _CheckpointStub(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "checkpoint_write"

    @property
    def description(self) -> str:
        return "checkpoint"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {"note": {"type": "string"}}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        if ctx is not None and ctx.note_checkpoint is not None:
            ctx.note_checkpoint(str(params.get("note", "")))
        return ToolResult(success=True, output="checkpoint: 记一行")


@pytest.mark.asyncio
async def test_r8_a_turn_that_only_wrote_a_checkpoint_is_not_tool_evidence(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    memory = MemoryService(
        store, extractor=_Extractor("项目用 uv 管理依赖"), config=MemoryConfig()
    )
    sessions = SessionService(tmp_path / "sessions")
    registry = ToolRegistry()
    registry.register(_CheckpointStub())
    loop = AgentLoop(
        _CheckpointThenAnswer(),
        sessions,
        registry,
        approval=AutoApproveCallback(),
        memory=memory,
        prompt=PromptAssembler("You are a coding assistant."),
        settings=_cfg(max_tool_iterations=3),
        workspace=str(tmp_path),
    )

    await loop.process_input(sessions.create(), "先记一行再干别的")
    await loop.flush_harvest()

    harvested = [note for note in store.all() if "uv" in note.text]
    assert harvested, "the harvest must still run"
    assert harvested[-1].evidence == "model", (
        "a checkpoint call is the turn writing to itself, not tool evidence (D-14)"
    )


# ---- 2026-10-17: the harvest is fire-and-forget (tail = one LLM call) -------
#
# Measured live: the turn's future stayed open 0-22s after ``turn.finished``,
# all of it ``await turn_memory.harvest`` -- one extractor LLM call.  Every
# waiter downstream (the wake latch's runway, promote's settle-wait) paid it.
# The harvest now runs after the response, like the skill review already does;
# the cursor only advances over what the extractor was actually shown.


class _GatedExtractor:
    """Parks inside the call until the gate opens; records every batch."""

    def __init__(self, gate) -> None:
        self.gate = gate
        self.seen: list[list[ChatMessage]] = []

    async def __call__(self, messages, conclusion):
        self.seen.append(list(messages))
        await self.gate.wait()
        return []


@pytest.mark.asyncio
async def test_process_input_returns_before_a_slow_harvest_finishes(tmp_path):
    import asyncio
    import time

    gate = asyncio.Event()
    extractor = _GatedExtractor(gate)
    memory = _memory(tmp_path, extractor)
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(_Engine(), sessions, memory)
    session = sessions.create()

    started = time.monotonic()
    text = await asyncio.wait_for(loop.process_input(session, "第一轮"), timeout=1.0)
    elapsed = time.monotonic() - started

    assert text == "好的"
    assert elapsed < 0.5, "the extractor is parked on the gate; the turn must not wait"
    for _ in range(100):  # the scheduled task needs a few ticks to reach the call
        if extractor.seen:
            break
        await asyncio.sleep(0.01)
    assert extractor.seen, "the harvest did start in the background"

    gate.set()
    await loop.flush_harvest()
    assert session.harvested_messages > 0, "the flushed harvest advanced the cursor"


@pytest.mark.asyncio
async def test_a_later_harvest_chains_behind_a_slow_one_without_repeating(tmp_path):
    import asyncio
    import time

    gate = asyncio.Event()
    extractor = _GatedExtractor(gate)
    memory = _memory(tmp_path, extractor)
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(_Engine(), sessions, memory)
    session = sessions.create()

    started = time.monotonic()

    async def three_turns():
        await loop.process_input(session, "第一轮关键字")
        await loop.process_input(session, "第二轮关键字")
        await loop.process_input(session, "第三轮关键字")

    await asyncio.wait_for(three_turns(), timeout=1.0)
    assert time.monotonic() - started < 1.0, "turns must not wait on parked harvests"

    gate.set()
    await loop.flush_harvest()

    batches = ["\n".join(m.content or "" for m in batch) for batch in extractor.seen]
    # Deltas may coalesce when turns outrun extraction (by design), but a
    # message is shown to the extractor exactly once, and order is preserved.
    for keyword in ("第一轮关键字", "第二轮关键字", "第三轮关键字"):
        total = sum(keyword in batch for batch in batches)
        assert total == 1, f"{keyword} appeared {total} times across {len(batches)} batches"
    first_batch_of = {
        keyword: next(i for i, batch in enumerate(batches) if keyword in batch)
        for keyword in ("第一轮关键字", "第二轮关键字", "第三轮关键字")
    }
    assert (
        first_batch_of["第一轮关键字"]
        <= first_batch_of["第二轮关键字"]
        <= first_batch_of["第三轮关键字"]
    ), first_batch_of


@pytest.mark.asyncio
async def test_the_cursor_advances_over_what_the_extractor_saw_not_what_exists_now(tmp_path):
    """The offset race the async schedule would otherwise open: a later turn
    appends while the extractor runs; advancing to ``len(session.messages)``
    would silently mark those messages harvested without ever showing them."""
    import asyncio

    gate = asyncio.Event()
    extractor = _GatedExtractor(gate)
    memory = _memory(tmp_path, extractor)
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(_Engine(), sessions, memory)
    session = sessions.create()

    await loop.process_input(session, "第一轮")
    for _ in range(100):  # the task chain needs a few ticks to reach the call
        if extractor.seen:
            break
        await asyncio.sleep(0.01)
    assert extractor.seen, "precondition: the harvest froze its delta"
    captured = len(session.messages)
    session.messages.append(
        ChatMessage(role="user", content="下一回合才该被看见的消息", timestamp="")
    )

    gate.set()
    await loop.flush_harvest()

    assert session.harvested_messages == captured, (
        "the cursor marks what the extractor was shown, not what exists now"
    )
    assert session.harvested_messages < len(session.messages)
