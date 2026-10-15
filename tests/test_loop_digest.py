"""Loop wiring for R6a: the digest ring, the tail block, and the handoff.

The fake tools below have names outside the permission gate's table, and unknown
tools are WARN (fail-closed), so the loops here carry ``AutoApproveCallback`` —
exactly what the non-interactive ``ask`` entry point uses.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import ToolChunk
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Always asks for the same tool, so the turn can only end on budget."""

    def __init__(self, tool_name: str | None = "poke") -> None:
        self.tool_name = tool_name
        self.requests: list[list[ChatMessage]] = []
        self.instructions: list[str | None] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        self.instructions.append(instructions)
        if self.tool_name is None:
            return StepResult(text="做完了", tool_calls=[], finish_reason="stop")
        arguments = (
            '{"note": "先补 runner 的用例"}'
            if self.tool_name == "checkpoint_write"
            else "{}"
        )
        return StepResult(
            text=None,
            tool_calls=[
                ToolCallRef(
                    id=f"c{len(self.requests)}", name=self.tool_name, arguments=arguments
                )
            ],
            finish_reason="tool_calls",
        )


class _Poke(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "poke"

    @property
    def description(self) -> str:
        return "poke"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="poked")


class _CheckpointTool(BaseTool):
    """A stand-in for checkpoint_write: it only writes the turn's note (R8a)."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "checkpoint_write"

    @property
    def description(self) -> str:
        return "checkpoint"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"note": {"type": "string"}}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        note = str(params.get("note", ""))
        if ctx is not None and ctx.note_checkpoint is not None:
            ctx.note_checkpoint(note)
        return ToolResult(success=True, output=f"remembered: {note}")


class _Sessions:
    def __init__(self) -> None:
        self.saved: list[SessionData] = []

    def save(self, session: SessionData) -> None:
        self.saved.append(session)

    def mark_pending_approval(self, session_id: str, request) -> None:
        """P3: stand-in for the marker the real SessionService writes beside
        the session file; this fake keeps nothing on disk."""
        return None


class _Compacting:
    """A stand-in context manager: rewrites the history (or does not) on demand."""

    def __init__(self, rewrite: bool) -> None:
        self.rewrite = rewrite

    async def compress_if_needed(self, messages, *, force=False, on_summary=None):
        if not self.rewrite:
            return messages
        return [*messages, ChatMessage(role="user", content="[compacted]", timestamp="")]


def _loop(engine: ChatEngine, context: Any = None, **overrides: Any) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Poke())
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(**overrides),
        context=context,
        workspace=".",
    )


def _session() -> SessionData:
    return SessionData(session_id="s1", title="", created_time="t", last_access_time="t")


def _last_request(engine: _Engine) -> str:
    return "\n".join(message.content or "" for message in engine.requests[-1])


@pytest.mark.asyncio
async def test_each_turn_appends_one_digest():
    loop = _loop(_Engine(tool_name=None))
    session = _session()
    await loop.process_input(session, "第一件事")
    await loop.process_input(session, "第二件事")
    assert [digest.turn for digest in session.turn_digests] == [1, 2]
    assert session.turn_count == 2
    assert "第一件事" in session.turn_digests[0].request


@pytest.mark.asyncio
async def test_digests_are_persisted_with_the_session():
    sessions = _Sessions()
    registry = ToolRegistry()
    registry.register(_Poke())
    loop = AgentLoop(
        _Engine(tool_name=None),
        sessions,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
    )
    await loop.process_input(_session(), "写点什么")
    assert sessions.saved[-1].turn_digests, "the ring must reach the session file"
    assert sessions.saved[-1].turn_count == 1


@pytest.mark.asyncio
async def test_budget_exhaustion_returns_the_handoff_not_an_empty_string():
    loop = _loop(_Engine(), max_tool_iterations=2)
    session = _session()
    answer = await loop.process_input(session, "让 make test 通过")
    assert answer, "the user must never get an empty string"
    assert "[回合未完成" in answer
    assert "让 make test 通过" in answer
    assert loop.last_outcome is not None
    assert loop.last_outcome.text is None  # 模型确实没说话
    assert loop.last_outcome.handoff == answer  # 交接在 outcome 上，不在 text 上
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_working_memory_is_injected_only_when_history_was_compacted():
    plain_engine = _Engine(tool_name=None)
    plain = _loop(plain_engine, _Compacting(rewrite=False))
    plain_session = _session()
    await plain.process_input(plain_session, "一")
    await plain.process_input(plain_session, "二")

    compact_engine = _Engine(tool_name=None)
    compacting = _loop(compact_engine, _Compacting(rewrite=True))
    compact_session = _session()
    await compacting.process_input(compact_session, "一")
    await compacting.process_input(compact_session, "二")

    assert "<working-memory>" not in _last_request(plain_engine)
    assert "<working-memory>" in _last_request(compact_engine)


@pytest.mark.asyncio
async def test_working_memory_is_a_tail_block_never_the_system_prompt():
    engine = _Engine(tool_name=None)
    loop = _loop(engine, _Compacting(rewrite=True))
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    assert "<working-memory>" in (engine.requests[-1][-1].content or "")
    assert all("<working-memory>" not in (item or "") for item in engine.instructions)


@pytest.mark.asyncio
async def test_a_completed_turn_keeps_text_and_leaves_handoff_empty():
    loop = _loop(_Engine(tool_name=None))
    answer = await loop.process_input(_session(), "打个招呼")
    assert answer == "做完了"
    assert loop.last_outcome is not None
    assert loop.last_outcome.text == "做完了"
    assert loop.last_outcome.handoff == ""


# ---- R8a: the model's own line travels tool -> books -> digest ------------


def _checkpoint_loop() -> tuple[AgentLoop, _Engine]:
    engine = _Engine(tool_name="checkpoint_write")
    loop = _loop(engine, _Compacting(rewrite=False), max_tool_iterations=2)
    loop.tools.register(_CheckpointTool())
    return loop, engine


@pytest.mark.asyncio
async def test_r8_the_tool_note_reaches_the_digest():
    loop, _engine_ = _checkpoint_loop()
    session = _session()
    await loop.process_input(session, "先写一行 checkpoint")
    assert session.turn_digests[-1].note == "先补 runner 的用例"
    assert session.turn_digests[-1].source == "model"


@pytest.mark.asyncio
async def test_r8_the_call_is_written_back_into_the_history():
    loop, _engine_ = _checkpoint_loop()
    session = _session()
    await loop.process_input(session, "先写一行 checkpoint")
    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert tool_messages
    assert "remembered" in (tool_messages[-1].content or "")


@pytest.mark.asyncio
async def test_r8_the_note_never_enters_the_system_prompt():
    loop, engine = _checkpoint_loop()
    await loop.process_input(_session(), "先写一行 checkpoint")
    assert all("先补 runner 的用例" not in (item or "") for item in engine.instructions)


class _ChunkyTool(BaseTool):
    """Emits a chunk and returns a normal result (R8c's shape)."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "chunky"

    @property
    def description(self) -> str:
        return "chunky"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        if ctx is not None:
            ctx.emit(
                ToolChunk(
                    call_id=ctx.call_id,
                    tool_name="chunky",
                    stream="stdout",
                    text="secret-chunk\n",
                    seq=0,
                )
            )
        return ToolResult(success=True, output="final-output")


@pytest.mark.asyncio
async def test_r8_a_tool_chunk_never_reaches_the_request_or_the_history():
    engine = _Engine(tool_name="chunky")
    loop = _loop(engine, _Compacting(rewrite=False), max_tool_iterations=2)
    loop.tools.register(_ChunkyTool())
    session = _session()

    await loop.process_input(session, "跑点东西")

    request_text = "\n".join(message.content or "" for message in engine.requests[-1])
    assert "secret-chunk" not in request_text
    assert all("secret-chunk" not in (message.content or "") for message in session.messages)
    # the final ToolResult is what the model gets, and it does land in the history
    assert any("final-output" in (message.content or "") for message in session.messages)
