"""TDD (M3-3): MemoryService — recall / remember / dream with injected extractor.

Never raises; offline fallback extracts recent user/assistant signal when no
extractor is provided.
"""

import asyncio
from types import SimpleNamespace

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.memory.note import MemoryNote
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


def _cfg(**overrides):
    base = {
        "enabled": True,
        "auto_extract": True,
        "consolidate_threshold": 3,
        "max_index_entries": 100,
        "max_per_turn_injections": 4,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(content):
    return ChatMessage(role="user", content=content, timestamp="t")


@pytest.mark.asyncio
async def test_recall_offline_keyword(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add("user prefers pytest for tests")
    store.add("deploy runs on linux")
    svc = MemoryService(store, config=_cfg())
    history = [_user("how do I run my pytest tests?")]
    recalls = await svc.recall(history, k=1)
    assert recalls == ["user prefers pytest for tests"]


@pytest.mark.asyncio
async def test_remember_injects_and_stores(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg())
    ids = await svc.remember([_user("remember python 3.10")], conclusion="py310")
    assert len(ids) == 1
    assert store.count == 1


@pytest.mark.asyncio
async def test_remember_never_raises(tmp_path):
    store = MemoryStore(tmp_path / "m.json")

    async def bad_extractor(*_a, **_k):
        raise RuntimeError("llm down")

    svc = MemoryService(store, extractor=bad_extractor, config=_cfg())
    ids = await svc.remember([_user("hi")], "concl")  # must not raise
    assert ids == []


@pytest.mark.asyncio
async def test_dream_threshold(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    seen = []

    async def extractor(messages, conclusion):
        seen.append(list(messages))
        return ["consolidated note"]

    # count(0) < threshold(10) -> nothing happens
    below = MemoryService(store, extractor=extractor, config=_cfg(consolidate_threshold=10))
    assert await below.dream([_user("a")]) == []

    # raise the count above a low threshold -> consolidation runs
    store.add("one")
    store.add("two")
    svc2 = MemoryService(store, extractor=extractor, config=_cfg(consolidate_threshold=1))
    ids = await svc2.dream([_user("context")])
    assert len(ids) == 1  # one new entry stored
    assert "consolidated note" in {e.text for e in store.all()}


@pytest.mark.asyncio
async def test_extractor_injected_for_remember(tmp_path):
    store = MemoryStore(tmp_path / "m.json")

    async def extractor(messages, conclusion):
        return [f"summary:{conclusion}"]

    svc = MemoryService(store, extractor=extractor, config=_cfg())
    await svc.remember([_user("x")], "the deal")
    assert "summary:the deal" in {e.text for e in store.all()}


# ---- R10b: the usage signal is wired -----------------------------------------


def test_r10b_a_hit_counts_as_a_use(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", importance=4,
                   created_time="2026-09-20T00:00:00+00:00")
    )
    service = MemoryService(store)

    hits = asyncio.run(service.recall_notes("依赖怎么装", k=3))

    assert [hit.note.id for hit in hits] == ["a"]
    assert store.get("a").hits == 1
    assert store.get("a").last_used != ""


def test_r10b_a_miss_touches_nothing(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", importance=4,
                   created_time="2026-09-20T00:00:00+00:00")
    )
    service = MemoryService(store)

    assert asyncio.run(service.recall_notes("Kubernetes 集群有多少节点", k=3)) == []
    assert store.get("a").hits == 0


def test_r10b_consolidation_can_now_promote_from_real_use(tmp_path):
    """`hits >= 3 -> importance + 1` was unreachable while nothing touched on recall."""
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", importance=3,
                   created_time="2026-09-20T00:00:00+00:00")
    )
    service = MemoryService(store)
    for _ in range(3):
        asyncio.run(service.recall_notes("依赖怎么装", k=3))

    report = asyncio.run(service.consolidate(force=True))

    assert "a" in report.promoted
    assert store.get("a").importance == 4
