"""TDD (R3b): the on-demand recall tool.

This is the cache-friendly recall channel (docs/r3-analysis.md 1.13): the model asks
for what it needs, the result is appended to the history as a normal tool message,
and every later turn keeps hitting the prompt cache on it.  The tool is read-only,
so the permission gate must allow it without a prompt.
"""

import pytest

from ai_coding.domain.message import ChatMessage, ToolResult
from ai_coding.memory.note import MemoryNote, NoteKind
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.tools.memory_search_tool import MemorySearchTool


def _service(tmp_path, *texts: str) -> MemoryService:
    store = MemoryStore(tmp_path / "m.json")
    for text in texts:
        store.add_note(MemoryNote(id=f"id-{abs(hash(text)) % 10_000}", text=text))
    return MemoryService(store)


@pytest.mark.asyncio
async def test_search_returns_structured_notes(tmp_path):
    svc = _service(tmp_path, "项目用 uv 管理依赖")
    tool = MemorySearchTool(svc)

    result = await tool.execute({"query": "依赖怎么装"})

    assert result.success is True
    assert result.data is not None
    notes = result.data["notes"]
    assert len(notes) == 1
    assert notes[0]["text"] == "项目用 uv 管理依赖"
    assert notes[0]["kind"] == "fact"
    assert 0.0 < notes[0]["score"] <= 1.5
    assert "uv" in result.output  # the model reads this


@pytest.mark.asyncio
async def test_empty_query_is_a_structured_error(tmp_path):
    tool = MemorySearchTool(_service(tmp_path))
    result = await tool.execute({"query": "   "})
    assert result.success is False
    assert result.error == "empty_query"
    assert result.data == {"query": ""}


@pytest.mark.asyncio
async def test_no_hits_is_a_successful_empty_answer(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "payment service timeout"))
    result = await tool.execute({"query": "how do I add a dependency"})
    assert result.success is True
    assert result.data == {"notes": []}


@pytest.mark.asyncio
async def test_kind_filter(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="项目用 uv 管理依赖", kind=NoteKind.FACT))
    store.add_note(
        MemoryNote(id="b", text="决定：依赖管理统一用 uv", kind=NoteKind.DECISION)
    )
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "uv 依赖", "kind": "decision"})

    assert [note["kind"] for note in result.data["notes"]] == ["decision"]


@pytest.mark.asyncio
async def test_tool_is_read_only_and_parallel_safe(tmp_path):
    tool = MemorySearchTool(_service(tmp_path))
    assert tool.name == "memory_search"
    assert tool.parallel_safe is True
    assert PermissionGate().check("memory_search", {}) == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_tool_survives_a_broken_memory(tmp_path):
    class _Broken:
        async def recall_notes(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    tool = MemorySearchTool(_Broken())
    result = await tool.execute({"query": "anything"})
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error == "memory_error"


@pytest.mark.asyncio
async def test_query_can_be_built_from_the_tool_context(tmp_path):
    """Without an explicit query the tool falls back to the parent turn's view."""
    svc = _service(tmp_path, "项目用 uv 管理依赖")
    tool = MemorySearchTool(svc)
    ctx = type(
        "C",
        (),
        {
            "messages": (ChatMessage(role="user", content="依赖怎么装", timestamp="t"),),
            "instructions": None,
        },
    )()

    result = await tool.execute({}, ctx)  # type: ignore[arg-type]

    assert result.success is True
    assert result.data["notes"]


# ---- R8b: filtering by where a note came from (D-4) ------------------------


# These tests assert *which ids come back per evidence filter*, not how they rank.
# The wording is chosen so every note clears the relevance floor: in a 3-note
# corpus idf is extreme, so a note that covers only the common bigram scores
# 0.221 and is gated out, which would fail these for the wrong reason.
def _evidence_service(tmp_path) -> MemoryService:
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="t1", text="构建用 uv", evidence="tool"))
    store.add_note(MemoryNote(id="m1", text="构建用 uv，改用别家", evidence="model"))
    store.add_note(MemoryNote(id="l1", text="构建用 uv，老数据", evidence=""))
    return MemoryService(store)


@pytest.mark.asyncio
async def test_r8_evidence_filter_keeps_only_tool_backed(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么", "evidence": "tool"})

    assert result.success is True
    assert result.data is not None
    assert [n["id"] for n in result.data["notes"]] == ["t1"]
    assert result.data["notes"][0]["evidence"] == "tool"


@pytest.mark.asyncio
async def test_r8_evidence_filter_model_means_not_tool(tmp_path):
    """Notes from before R6d have no evidence field; they are not 'tool'."""
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么", "evidence": "model"})

    assert {n["id"] for n in result.data["notes"]} == {"m1", "l1"}


@pytest.mark.asyncio
async def test_r8_evidence_empty_is_exactly_today(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么"})

    assert {n["id"] for n in result.data["notes"]} == {"t1", "m1", "l1"}


@pytest.mark.asyncio
async def test_r8_unknown_evidence_is_an_error_not_a_silent_pass(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么", "evidence": "maybe"})

    assert result.success is False
    assert result.error == "bad_evidence"
    assert result.data == {"evidence": "maybe"}
