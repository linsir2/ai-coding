"""R7e-1: the model can write a note, through the same door as the harvest.

`_reconcile` has been the only write path since R3 (dedupe, similarity merge,
explicit supersede, protection, eviction, evidence), so the model's own notes go
through it too - a second write path is how invariants get bypassed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.config.models import MemoryConfig
from ai_coding.memory.note import NoteKind
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


def _service(tmp_path: Path, **overrides) -> MemoryService:
    return MemoryService(
        MemoryStore(tmp_path / ".memory"),
        config=MemoryConfig(**overrides),
    )


def test_write_note_adds_a_note(tmp_path: Path):
    service = _service(tmp_path)
    report = service.write_note("构建改用 uv，不用 poetry", kind="decision", importance=4)
    assert report.added
    note = service.store.get(report.added[0])
    assert note is not None
    assert note.kind is NoteKind.DECISION
    assert note.importance == 4


def test_write_note_records_evidence_model(tmp_path: Path):
    service = _service(tmp_path)
    report = service.write_note("用户偏好中文回答")
    note = service.store.get(report.added[0])
    assert note is not None and note.evidence == "model"


def test_write_note_dedupes_the_same_text(tmp_path: Path):
    service = _service(tmp_path)
    first = service.write_note("CI 缓存改走 uv")
    second = service.write_note("CI 缓存改走 uv")
    assert not second.added
    assert second.skipped == first.added  # 返回既有 id，不新增
    assert len(service.store.all()) == 1


def test_write_note_supersedes_an_explicit_id(tmp_path: Path):
    service = _service(tmp_path)
    old = service.write_note("依赖用 poetry")
    new = service.write_note("依赖改用 uv", supersedes=old.added[0])
    assert new.superseded
    retired = service.store.get(old.added[0])
    assert retired is not None and retired.is_active is False


def test_write_note_ignores_an_unknown_target_id(tmp_path: Path):
    service = _service(tmp_path)
    report = service.write_note("随便一条", supersedes="does-not-exist")
    assert not report.added
    assert not report.superseded
    assert report.skipped  # _reconcile 的 supersede 分支失败 → skipped


def test_write_note_is_a_noop_when_memory_is_disabled(tmp_path: Path):
    service = _service(tmp_path, enabled=False)
    report = service.write_note("不该写进去")
    assert not report.changed
    assert service.store.all() == []


def test_write_note_rejects_empty_text(tmp_path: Path):
    service = _service(tmp_path)
    assert not service.write_note("   ").changed


def test_write_note_evicts_above_the_cap(tmp_path: Path):
    service = _service(tmp_path, max_index_entries=2)
    # 文本必须真的不相似：相似度 ≥ dedupe_similarity 会被合并，而不是淘汰
    for text in ("构建用 uv", "验收命令是 make check", "回答一律用中文"):
        service.write_note(text)
    assert len(service.store.all()) == 2


def test_a_written_note_is_recallable(tmp_path: Path):
    import asyncio

    service = _service(tmp_path)
    service.write_note("这个仓库的验收命令是 make check", kind="fact")
    hits = asyncio.run(service.recall_notes("验收命令是什么", k=5))
    assert any("make check" in hit.note.text for hit in hits)


# ---- the tool ---------------------------------------------------------------


def _tool(tmp_path: Path) -> tuple[object, MemoryService]:
    from ai_coding.tools.memory_write_tool import MemoryWriteTool

    service = _service(tmp_path)
    return MemoryWriteTool(service), service


@pytest.mark.asyncio
async def test_the_tool_requires_text(tmp_path: Path):
    from ai_coding.tools.base import ToolContext

    tool, _service_obj = _tool(tmp_path)
    result = await tool.execute({}, ToolContext())
    assert result.success is False and result.error == "missing_text"


@pytest.mark.asyncio
async def test_the_tool_rejects_an_unknown_kind(tmp_path: Path):
    from ai_coding.tools.base import ToolContext

    tool, _service_obj = _tool(tmp_path)
    result = await tool.execute({"text": "x", "kind": "gossip"}, ToolContext())
    assert result.success is False and result.error == "unknown_kind"


@pytest.mark.asyncio
async def test_the_tool_truncates_a_long_note_and_says_so(tmp_path: Path):
    from ai_coding.tools.base import ToolContext

    tool, service = _tool(tmp_path)
    result = await tool.execute({"text": "x" * 900}, ToolContext())
    assert result.success is True
    assert result.data["truncated"] is True
    assert len(service.store.all()[0].text) <= 501


@pytest.mark.asyncio
async def test_the_tool_returns_the_note_id_and_the_action(tmp_path: Path):
    from ai_coding.tools.base import ToolContext

    tool, _service_obj = _tool(tmp_path)
    result = await tool.execute({"text": "CI 缓存改走 uv", "kind": "decision"}, ToolContext())
    assert result.success is True
    assert result.data["action"] == "added"
    assert result.data["note_id"]
    assert result.data["kind"] == "decision"


@pytest.mark.asyncio
async def test_a_duplicate_write_is_not_an_error(tmp_path: Path):
    from ai_coding.tools.base import ToolContext

    tool, _service_obj = _tool(tmp_path)
    first = await tool.execute({"text": "同一句话"}, ToolContext())
    second = await tool.execute({"text": "同一句话"}, ToolContext())
    assert second.success is True
    assert second.data["action"] == "skipped"
    assert second.data["note_id"] == first.data["note_id"]


@pytest.mark.asyncio
async def test_the_tool_survives_a_service_exception(tmp_path: Path):
    from ai_coding.tools.base import ToolContext
    from ai_coding.tools.memory_write_tool import MemoryWriteTool

    class _Broken:
        def write_note(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    result = await MemoryWriteTool(_Broken()).execute({"text": "x"}, ToolContext())
    assert result.success is False and result.error == "memory_error"


@pytest.mark.asyncio
async def test_the_tool_passes_the_turn_and_session_through():
    from ai_coding.memory.service import HarvestReport
    from ai_coding.tools.base import ToolContext
    from ai_coding.tools.memory_write_tool import MemoryWriteTool

    seen: dict[str, object] = {}

    class _Recorder:
        def write_note(self, text, **kwargs):
            seen.update(kwargs, text=text)
            return HarvestReport(added=("note-1",))

    tool = MemoryWriteTool(_Recorder())
    await tool.execute({"text": "x"}, ToolContext(session_id="s1", turn=7))
    assert seen["session_id"] == "s1"
    assert seen["turn"] == 7


def test_a_tool_context_carries_the_turn_number_by_default_zero():
    from ai_coding.tools.base import ToolContext

    assert ToolContext().turn == 0


# ---- the gate and the wiring -------------------------------------------------


def test_the_gate_allows_memory_write():
    from ai_coding.security.permission_gate import PermissionDecision, PermissionGate

    assert PermissionGate().check("memory_write", {}) == PermissionDecision.ALLOW


def test_a_subagent_stack_has_no_memory_tools(tmp_path: Path):
    from ai_coding.config.models import AppConfig
    from ai_coding.tools.builder import build_tool_registry

    cfg = AppConfig()
    child = build_tool_registry(cfg, str(tmp_path), None, include_subagent=False)
    assert child.get("memory_write") is None  # Q3: 子智能体没有
    assert child.get("memory_search") is None


@pytest.mark.asyncio
async def test_the_composition_root_registers_memory_write(tmp_path: Path):
    from ai_coding.ai.engine import ChatEngine
    from ai_coding.cli import _build_loop, _build_tool_registry
    from ai_coding.config.models import AppConfig

    class _Engine(ChatEngine):
        async def step(self, *args, **kwargs):  # pragma: no cover - never called
            raise AssertionError("the loop must not run")

        async def aclose(self) -> None:
            return None

    class _Sessions:
        def save(self, session) -> None:
            return None

    cfg = AppConfig()
    registry = _build_tool_registry(cfg, str(tmp_path), None)
    _build_loop(_Engine(), _Sessions(), registry, cfg, str(tmp_path), None)
    assert registry.get("memory_write") is not None
    assert registry.get("memory_search") is not None
