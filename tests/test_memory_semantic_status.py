"""N-backlog 领域 F / M-F1: the semantic tier must be observable.

Measured: `_semantic_scores` returned ``(None, False)`` for **all three** of
"off", "unconfigured" and "the endpoint cannot embed" -- so an operator who set
``enabled=True`` against an endpoint without ``/embeddings`` got no signal that
their configuration was not working (service.py:443-450).

The four states are: ``off`` / ``endpoint_unsupported`` / ``on`` / ``error``.
"""

from __future__ import annotations

import pytest

from ai_coding.memory.note import MemoryNote
from ai_coding.memory.service import MemoryService


def _service(tmp_path, *, enabled: bool, transport=None) -> MemoryService:
    from ai_coding.memory.embeddings import EmbeddingIndex
    from ai_coding.memory.service import _default_config
    from ai_coding.memory.store import MemoryStore

    overrides = {
        "provider": "p",
        "model": "m",
        "dimensions": 8,
        "min_cosine": 0.6,
        "timeout_seconds": 1.0,
    }
    index = None
    if transport is not None:
        index = EmbeddingIndex(
            _embeddings_config(enabled, overrides),
            {"p": _model()},
            transport=transport,
        )
    return MemoryService(
        store=MemoryStore(tmp_path / "memory.json"),
        # `_default_config`, not a dict: `_get` reads with `getattr`, so a dict
        # config is silently ignored and every key falls back to default.
        config=_default_config(
            max_index_entries=200,
            recall_timeout_seconds=0.0,
        ),
        embeddings=index,
    )


def _embeddings_config(enabled: bool, overrides: dict):
    from ai_coding.config.models import EmbeddingsConfig

    return EmbeddingsConfig(enabled=enabled, **overrides)


def _model():
    from ai_coding.domain.model_config import ModelConfig

    return ModelConfig(name="p", base_url="https://example.invalid/v1", api_key="k")


async def _ok_transport(model: str, texts: list[str]):  # noqa: ANN001, ANN202
    return [[0.0] * 8 for _ in texts]


async def _unsupported_transport(model: str, texts: list[str]):  # noqa: ANN001, ANN202
    from ai_coding.memory.embeddings import EmbeddingsUnsupportedError

    raise EmbeddingsUnsupportedError("404")


async def _boom_transport(model: str, texts: list[str]):  # noqa: ANN001, ANN202
    raise RuntimeError("mid-call failure")


@pytest.mark.asyncio
async def test_a_disabled_tier_is_off(tmp_path):
    svc = _service(tmp_path, enabled=False)
    hits, diag = await svc.recall_with_diagnostics("anything")
    assert diag.semantic_status == "off"


@pytest.mark.asyncio
async def test_an_endpoint_that_cannot_embed_is_reported(tmp_path):
    """The gap this milestone closes: configured, but the endpoint cannot."""
    svc = _service(tmp_path, enabled=True, transport=_unsupported_transport)
    hits, diag = await svc.recall_with_diagnostics("anything")
    assert diag.semantic_status == "endpoint_unsupported"


@pytest.mark.asyncio
async def test_a_working_endpoint_is_on(tmp_path):
    svc = _service(tmp_path, enabled=True, transport=_ok_transport)
    hits, diag = await svc.recall_with_diagnostics("anything")
    assert diag.semantic_status == "on"


@pytest.mark.asyncio
async def test_a_mid_call_failure_is_error(tmp_path):
    svc = _service(tmp_path, enabled=True, transport=_boom_transport)
    hits, diag = await svc.recall_with_diagnostics("anything")
    assert diag.semantic_status == "error"


@pytest.mark.asyncio
async def test_the_unsupported_endpoint_is_probed_once_per_process(tmp_path):
    """The probe latches: one failed call, not one per recall."""
    calls: list[str] = []

    async def _counting(model: str, texts: list[str]):  # noqa: ANN001, ANN202
        calls.append(model)
        from ai_coding.memory.embeddings import EmbeddingsUnsupportedError

        raise EmbeddingsUnsupportedError("404")

    svc = _service(tmp_path, enabled=True, transport=_counting)
    svc.store.add_note(MemoryNote(id="n1", text="a fact"))   # empty store short-circuits
    for _ in range(3):
        await svc.recall_with_diagnostics("anything")
    assert len(calls) == 1, f"expected one probe, got {len(calls)}"


def test_the_calibration_curve_is_produced(capsys):
    """U5: the calibration script is a pipeline test, not a quality proof.

    A fake transport's cosine is arbitrary, so the *recommended* threshold has
    no calibration meaning -- what is testable is that the sweep runs, the
    curve is printed, FP@5 is reported and the effective domain is declared.
    """
    from scripts import calibrate_memory_threshold

    assert calibrate_memory_threshold.main([]) == 0
    out = capsys.readouterr().out
    assert "baseline FP@5" in out, "FP@5 must be reported next to the curve"
    assert "有效域" in out, "the effective domain must be declared"
    assert "0.30" in out, "the sweep must actually run"
    # The sweep's own denominator must be the answerable subset: `noanswer` and
    # `trap` cases have no relevant note, so mixing them in would measure FP on
    # cases the metric is not defined on.
    from ai_coding.memory.benchmark import load_corpus
    from scripts.retrieval_cases import CORPUS, NOW

    _, cases = load_corpus(CORPUS, now=NOW)
    answerable = len([case for case in cases if case.relevant])
    assert f"answerable: {answerable}" in out, (
        f"expected answerable: {answerable}, got {out.splitlines()[1]!r}"
    )
