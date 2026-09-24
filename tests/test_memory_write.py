"""R7e-1: the model can write a note, through the same door as the harvest.

`_reconcile` has been the only write path since R3 (dedupe, similarity merge,
explicit supersede, protection, eviction, evidence), so the model's own notes go
through it too - a second write path is how invariants get bypassed.
"""

from __future__ import annotations

from pathlib import Path

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
