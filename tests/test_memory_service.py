"""TDD (M3-3): MemoryService — recall / remember / dream with injected extractor.

Never raises; offline fallback extracts recent user/assistant signal when no
extractor is provided.
"""

from types import SimpleNamespace

import pytest

from ai_coding.domain.message import ChatMessage
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
