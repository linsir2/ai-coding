"""TDD (R10d-4): the service wires the semantic signal, or it does not.

Every test here constructs the service with a fake-transport index, so none of
them needs a network.  The one thing only a real endpoint can prove — that the
numbers move — belongs to the acceptance script, not here.
"""

import asyncio

from ai_coding.config.models import EmbeddingsConfig
from ai_coding.domain.model_config import ModelConfig
from ai_coding.memory.embeddings import EmbeddingIndex, EmbeddingsUnsupportedError
from ai_coding.memory.note import MemoryNote
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


def _models() -> dict:
    return {
        "qwen": ModelConfig(
            name="qwen",
            base_url="https://dashscope.example/v1",
            api_key="k",
        )
    }


def _note(note_id: str, text: str) -> MemoryNote:
    return MemoryNote(
        id=note_id, text=text, created_time="2026-09-01T00:00:00+00:00"
    )


def _service(tmp_path, *texts: str, **over) -> tuple:
    store = MemoryStore(tmp_path / "m.json")
    for index, text in enumerate(texts):
        store.add_note(_note(f"n{index}", text))
    cfg = EmbeddingsConfig(enabled=True, provider="qwen", dimensions=2, **over)
    calls: list = []

    async def transport(model: str, inputs: list) -> list:
        calls.append((model, list(inputs)))
        return [[1.0, 0.0] for _ in inputs]

    index = EmbeddingIndex(cfg, _models(), transport=transport,
                           cache_path=tmp_path / "e.json")
    return MemoryService(store, config=None, embeddings=index), store, calls


def test_r10d_disabled_means_the_transport_is_never_touched(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(_note("a", "项目依赖统一用 uv 管理"))
    cfg = EmbeddingsConfig(enabled=False, provider="qwen")
    calls: list = []

    async def transport(model: str, inputs: list) -> list:
        calls.append((model, list(inputs)))
        raise AssertionError("must not be called")

    service = MemoryService(
        store,
        embeddings=EmbeddingIndex(cfg, _models(), transport=transport,
                                   cache_path=tmp_path / "e.json"),
    )
    hits, diagnostics = asyncio.run(service.recall_with_diagnostics("依赖怎么装"))

    assert calls == []
    assert diagnostics.semantic_unavailable is False
    assert [hit.note.id for hit in hits] == ["a"]


def test_r10d_a_cosine_above_the_floor_rescues_a_zero_overlap_note(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(_note("n0", "项目依赖统一用 uv 管理"))
    # Note ``b`` exists only so the query vocabulary is non-empty: without a
    # single shared word the ranker returns before any gate.  It is old and
    # unimportant so its score cannot set a band floor above the rescued note.
    store.add_note(
        MemoryNote(id="n1", text="发票很有用", importance=1,
                   created_time="2026-01-01T00:00:00+00:00")
    )
    cfg = EmbeddingsConfig(enabled=True, provider="qwen", dimensions=2)

    async def transport(model: str, inputs: list) -> list:
        return [[1.0, 0.0] for _ in inputs]

    service = MemoryService(
        store,
        embeddings=EmbeddingIndex(cfg, _models(), transport=transport,
                                   cache_path=tmp_path / "e.json"),
    )

    async def rescue():
        async def _fake_cosine(query: str, notes) -> dict:
            return {"n0": 0.9}

        service._embeddings.cosine_scores = _fake_cosine  # type: ignore[method-assign]
        return await service.recall_with_diagnostics("发票抬头是多少")

    hits, _ = asyncio.run(rescue())
    ids = [hit.note.id for hit in hits]
    assert "n0" in ids
    rescued = [hit for hit in hits if hit.note.id == "n0"][0]
    assert rescued.relevance == 0.0  # kept by cosine alone, not by coverage


def test_r10d_a_dead_endpoint_is_silent_not_unavailable(tmp_path):
    service, _, calls = _service(tmp_path, "项目依赖统一用 uv 管理")

    async def fail(model: str, inputs: list) -> list:
        calls.append((model, list(inputs)))
        raise EmbeddingsUnsupportedError("POST /embeddings -> 404")

    service._embeddings._transport = fail  # type: ignore[attr-defined]
    hits, diagnostics = asyncio.run(service.recall_with_diagnostics("依赖怎么装"))

    assert diagnostics.semantic_unavailable is False
    assert [hit.note.id for hit in hits] == ["n0"]


def test_r10d_a_network_failure_marks_unavailable_but_still_answers(tmp_path):
    service, _, _ = _service(tmp_path, "项目依赖统一用 uv 管理")

    async def fail(model: str, inputs: list) -> list:
        raise TimeoutError("timed out")

    service._embeddings._transport = fail  # type: ignore[attr-defined]
    hits, diagnostics = asyncio.run(service.recall_with_diagnostics("依赖怎么装"))

    assert diagnostics.semantic_unavailable is True
    assert [hit.note.id for hit in hits] == ["n0"]


def test_r10d_no_index_means_no_semantic_and_no_crash(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(_note("a", "项目依赖统一用 uv 管理"))
    service = MemoryService(store)

    hits, diagnostics = asyncio.run(service.recall_with_diagnostics("依赖怎么装"))

    assert diagnostics.semantic_unavailable is False
    assert [hit.note.id for hit in hits] == ["a"]


def test_r10d_off_is_byte_identical_to_absent(tmp_path):
    """I8: the payload must not change when the tier is off."""
    from ai_coding.tools.memory_search_tool import MemorySearchTool

    def build(enabled: bool):
        store = MemoryStore(tmp_path / f"{enabled}.json")
        store.add_note(_note("a", "项目依赖统一用 uv 管理"))
        cfg = EmbeddingsConfig(enabled=enabled, provider="qwen", dimensions=2)
        calls: list = []

        async def transport(model: str, inputs: list) -> list:
            calls.append((model, list(inputs)))
            return [[1.0, 0.0] for _ in inputs]

        service = MemoryService(
            store,
            embeddings=EmbeddingIndex(cfg, _models(), transport=transport,
                                       cache_path=tmp_path / f"{enabled}-e.json"),
        )
        return MemorySearchTool(service), calls

    on_tool, on_calls = build(True)
    off_tool, off_calls = build(False)
    # The "on" index is never consulted for this query shape… instead compare
    # off against a service with no index at all.
    store = MemoryStore(tmp_path / "plain.json")
    store.add_note(_note("a", "项目依赖统一用 uv 管理"))
    plain = MemorySearchTool(MemoryService(store))

    off = asyncio.run(off_tool.execute({"query": "依赖怎么装"}))
    base = asyncio.run(plain.execute({"query": "依赖怎么装"}))

    assert off.output == base.output
    assert off.data == base.data
    assert off_calls == []


def test_r10d_a_hit_still_counts_as_a_use_on_the_semantic_path(tmp_path):
    service, store, _ = _service(tmp_path, "项目依赖统一用 uv 管理")

    asyncio.run(service.recall_with_diagnostics("依赖怎么装"))

    assert store.get("n0").hits == 1


def test_r10d_recall_notes_delegates_so_it_gets_semantic_too(tmp_path):
    service, _, _ = _service(tmp_path, "项目依赖统一用 uv 管理")

    hits = asyncio.run(service.recall_notes("依赖怎么装"))

    assert [hit.note.id for hit in hits] == ["n0"]
