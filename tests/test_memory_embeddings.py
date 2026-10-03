"""TDD (R10d-1): the embeddings configuration.

The semantic tier is optional and off by default, so its configuration must be
valid and inert when absent.  These tests pin the shape before any network code
exists.
"""

import asyncio
import math

import pytest

from ai_coding.config.models import EmbeddingsConfig, MemoryConfig
from ai_coding.domain.model_config import ModelConfig
from ai_coding.memory.embeddings import EmbeddingIndex, EmbeddingsUnsupportedError
from ai_coding.memory.note import MemoryNote


def test_r10d_embeddings_are_off_by_default():
    cfg = MemoryConfig()

    assert cfg.embeddings.enabled is False
    assert cfg.embeddings.provider == ""
    assert cfg.embeddings.min_cosine == 0.60
    assert cfg.embeddings.cache_path == ".memory/embeddings.json"
    assert cfg.embeddings.timeout_seconds == 10.0


def test_r10d_embeddings_config_survives_unknown_keys():
    """Old config files must keep loading after this round adds a block."""
    cfg = MemoryConfig.model_validate({"embeddings": {"enabled": True, "bogus": 1}})

    assert cfg.embeddings.enabled is True


def test_r10d_memory_config_without_embeddings_block_still_loads():
    cfg = MemoryConfig.model_validate({})

    assert cfg.embeddings.enabled is False


def test_r10d_dimensions_zero_means_endpoint_default():
    cfg = EmbeddingsConfig(dimensions=0)

    assert cfg.dimensions == 0


def test_r10d_explicit_old_defaults_keep_old_behaviour():
    """A config that spells out the old world must not change meaning."""
    cfg = MemoryConfig.model_validate(
        {"recall_min_relevance": 0.05, "recall_relative_band": 0.55}
    )

    assert cfg.recall_min_relevance == 0.05
    assert cfg.embeddings.enabled is False


# ---- R10d-2: the optional vector index (fake transport, zero network) ----


def _models() -> dict:
    return {
        "qwen": ModelConfig(
            name="qwen",
            base_url="https://dashscope.example/v1",
            api_key="k",
        )
    }


def _cfg(**over) -> EmbeddingsConfig:
    base = {"enabled": True, "provider": "qwen", "dimensions": 2}
    base.update(over)
    return EmbeddingsConfig(**base)


def _note(note_id: str, text: str) -> MemoryNote:
    return MemoryNote(id=note_id, text=text, created_time="2026-09-01T00:00:00+00:00")


def _fake(vectors: dict) -> tuple:
    """A transport that serves canned vectors and counts its calls."""
    calls: list = []

    async def transport(model: str, inputs: list) -> list:
        calls.append((model, list(inputs)))
        return [vectors[text] for text in inputs]

    return transport, calls


def test_r10d_cosine_is_correct_by_hand(tmp_path):
    transport, _ = _fake({})
    index = EmbeddingIndex(_cfg(), _models(), transport=transport,
                           cache_path=tmp_path / "e.json")

    assert index.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert index.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert index.cosine([1.0, 1.0], [1.0, 0.0]) == pytest.approx(1 / math.sqrt(2))
    assert index.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_r10d_vectors_are_cached_and_missing_ones_computed(tmp_path):
    transport, calls = _fake({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    index = EmbeddingIndex(_cfg(), _models(), transport=transport,
                           cache_path=tmp_path / "e.json")

    first = asyncio.run(index.vectors_for([_note("1", "a"), _note("2", "b")]))
    second = asyncio.run(index.vectors_for([_note("1", "a"), _note("2", "b")]))

    assert first == second
    assert len(calls) == 1  # the second call served everything from disk


def test_r10d_changing_model_dimensions_or_endpoint_recomputes(tmp_path):
    transport, calls = _fake({"a": [1.0, 0.0]})
    kwargs = {"transport": transport, "cache_path": tmp_path / "e.json"}
    asyncio.run(EmbeddingIndex(_cfg(), _models(), **kwargs).vectors_for([_note("1", "a")]))

    for over in ({"model": "other"}, {"dimensions": 512}, {}):
        models = _models()
        if not over:
            models["qwen"] = ModelConfig(name="qwen", base_url="https://other.example/v1",
                                         api_key="k")
        asyncio.run(EmbeddingIndex(_cfg(**over), models, **kwargs).vectors_for([_note("1", "a")]))

    assert len(calls) == 4  # initial + one per changed key component


def test_r10d_a_corrupt_cache_rebuilds_as_empty(tmp_path):
    path = tmp_path / "e.json"
    path.write_text("not json", encoding="utf-8")
    transport, calls = _fake({"a": [1.0, 0.0]})
    index = EmbeddingIndex(_cfg(), _models(), transport=transport, cache_path=path)

    vectors = asyncio.run(index.vectors_for([_note("1", "a")]))

    assert vectors
    assert len(calls) == 1


def test_r10d_a_dimension_mismatch_discards_and_recomputes(tmp_path):
    transport, calls = _fake({"a": [1.0, 0.0]})
    index = EmbeddingIndex(_cfg(), _models(), transport=transport,
                           cache_path=tmp_path / "e.json")
    asyncio.run(index.vectors_for([_note("1", "a")]))

    other = EmbeddingIndex(_cfg(dimensions=3), _models(), transport=transport,
                           cache_path=tmp_path / "e.json")
    asyncio.run(other.vectors_for([_note("1", "a")]))

    assert len(calls) == 2


def test_r10d_empty_query_scores_nothing(tmp_path):
    transport, calls = _fake({})
    index = EmbeddingIndex(_cfg(), _models(), transport=transport,
                           cache_path=tmp_path / "e.json")

    assert asyncio.run(index.cosine_scores("", [_note("1", "a")])) == {}
    assert calls == []


def test_r10d_available_needs_config_and_a_known_provider(tmp_path):
    kwargs = {"transport": _fake({})[0], "cache_path": tmp_path / "e.json"}
    assert EmbeddingIndex(_cfg(), _models(), **kwargs).available() is True
    assert EmbeddingIndex(EmbeddingsConfig(), _models(), **kwargs).available() is False
    assert EmbeddingIndex(_cfg(provider="ghost"), _models(), **kwargs).available() is False
    assert EmbeddingIndex(_cfg(provider=""), _models(), **kwargs).available() is False


def test_r10d_a_404_latches_the_provider_and_stops_hitting_it(tmp_path):
    """An endpoint without /embeddings must not cost a round trip per search."""
    calls: list = []

    async def transport(model: str, inputs: list) -> list:
        calls.append((model, list(inputs)))
        raise EmbeddingsUnsupportedError("POST /embeddings -> 404")

    index = EmbeddingIndex(_cfg(), _models(), transport=transport,
                           cache_path=tmp_path / "e.json")
    # The probe itself raises unsupported, so the very first check already
    # latches: True means "yes, it lacks the capability".
    assert asyncio.run(index.provider_lacks_embeddings()) is True
    assert asyncio.run(index.provider_lacks_embeddings()) is True
    assert len(calls) == 1  # probed once, then short-circuited
