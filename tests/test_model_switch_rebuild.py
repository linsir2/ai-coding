"""``/model`` 必须真的换掉那条连接（H3 红相）。

今天的缺陷：``AgentLoop.set_model`` 只写 ``engine.model_config``，而
``OpenAIChatEngine._client`` 在构造时就把 ``base_url`` / ``api_key`` 固定了。
于是 ``/model`` 换到另一个 provider 之后，请求仍然发往旧端点、带旧 key——
命令打印 "switched"，行为是假的。

同一类缺陷还藏在压缩摘要与记忆抽取里：``cli._llm_helpers_for`` 把
``engine.model_config`` 烘进了闭包，切换后它们继续用旧模型。

所以这些用例断言的是**连接本身**与**闭包真正收到的模型**，不是
``model_config`` 那个字段——后者今天就是对的，断言它只能测出一个假象。
"""

from __future__ import annotations

import asyncio

import pytest

import ai_coding.ai.llm_helpers as llm_helpers
from ai_coding.ai.factory import build_engine
from ai_coding.ai.openai_engine import OpenAIChatEngine
from ai_coding.config.models import AIConfig, MemoryConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.context_compression import ContextManager
from ai_coding.domain.model_config import ModelConfig
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


def _models() -> dict[str, ModelConfig]:
    return {
        "alpha": ModelConfig(
            name="alpha-v1", base_url="https://a.test/v1", api_key="ka"
        ),
        "beta": ModelConfig(
            name="beta-v1", base_url="https://b.test/v1", api_key="kb"
        ),
    }


def _chained_models() -> dict[str, ModelConfig]:
    """alpha -> beta -> gamma, so a chain is what the factory builds."""
    return {
        "alpha": ModelConfig(
            name="alpha-v1",
            base_url="https://a.test/v1",
            api_key="ka",
            fallback_models=("beta",),
        ),
        "beta": ModelConfig(
            name="beta-v1",
            base_url="https://b.test/v1",
            api_key="kb",
            fallback_models=("gamma",),
        ),
        "gamma": ModelConfig(
            name="gamma-v1", base_url="https://g.test/v1", api_key="kg"
        ),
        # Not on the chain on purpose: the old setter refused this one outright.
        "delta": ModelConfig(
            name="delta-v1", base_url="https://d.test/v1", api_key="kd"
        ),
    }


class _Recorder:
    """An engine that records what happened to it (the retired one)."""

    def __init__(self, model_config: ModelConfig) -> None:
        self.model_config = model_config
        self.on_retry = None
        self.on_fallback = None
        self.closed = False

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        raise AssertionError("no model call expected")

    async def aclose(self) -> None:
        self.closed = True


def _loop(tmp_path, *, models=None, engine=None, settings=None, **over):
    table = models if models is not None else _models()
    if engine is None:
        engine = OpenAIChatEngine(table["alpha"])
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        settings=settings or AIConfig(),
        models=table,
        default_model="alpha",
        **over,
    )


def _url(engine) -> str:
    return str(engine._client.base_url).rstrip("/")


# ---- 连接本身 ---------------------------------------------------------------


def test_switching_providers_moves_the_client(tmp_path):
    loop = _loop(tmp_path)
    assert _url(loop.engine) == "https://a.test/v1"

    loop.set_model("beta")

    assert _url(loop.engine) == "https://b.test/v1"


def test_switching_rebuilds_rather_than_mutating(tmp_path):
    loop = _loop(tmp_path)
    old = loop.engine

    loop.set_model("beta")

    assert loop.engine is not old
    assert loop.engine.model_config.name == "beta-v1"


def test_the_rebuilt_engine_keeps_the_retry_and_fallback_observers(tmp_path):
    loop = _loop(tmp_path)
    retry, fallback = loop.engine.on_retry, loop.engine.on_fallback

    loop.set_model("beta")

    assert loop.engine.on_retry is retry
    assert loop.engine.on_fallback is fallback


def test_the_rebuilt_engine_keeps_stream_usage(tmp_path):
    loop = _loop(tmp_path, settings=AIConfig(stream_usage=False))

    loop.set_model("beta")

    assert isinstance(loop.engine, OpenAIChatEngine)
    assert loop.engine._stream_usage is False


def test_switching_to_a_model_outside_the_chain_works(tmp_path):
    table = _chained_models()
    loop = _loop(tmp_path, models=table, engine=build_engine(table, "alpha"))

    loop.set_model("delta")

    assert loop.engine.model_config.name == "delta-v1"


def test_the_target_models_own_chain_is_used(tmp_path):
    table = _chained_models()
    loop = _loop(tmp_path, models=table, engine=build_engine(table, "alpha"))

    loop.set_model("beta")

    assert [e.model_config.name for e in loop.engine._engines] == [
        "beta-v1",
        "gamma-v1",
    ]


def test_unknown_model_still_raises_value_error(tmp_path):
    loop = _loop(tmp_path)

    with pytest.raises(ValueError, match="Unknown model"):
        loop.set_model("nonexistent")


def test_switching_without_context_or_memory_is_fine(tmp_path):
    loop = _loop(tmp_path)
    assert loop.context is None and loop.memory is None

    loop.set_model("beta")

    assert loop.engine.model_config.name == "beta-v1"


# ---- 压缩与记忆抽取 ---------------------------------------------------------


def _context(tmp_path, summarizer=None):
    return ContextManager(
        AIConfig(), transcripts_root=tmp_path, summarizer=summarizer, context_window=8000
    )


def _memory(tmp_path, extractor=None):
    return MemoryService(
        MemoryStore(tmp_path / "memory.json"),
        extractor=extractor,
        config=MemoryConfig(),
    )


@pytest.mark.asyncio
async def test_switching_repoints_compaction_and_memory(tmp_path, monkeypatch):
    """The two helpers are closures over a baked-in model_config.

    Asserting "a new object" is not enough — a stale closure is also a new
    object.  So the closure is actually run with ``_chat`` stubbed, and the
    config it hands over is what gets checked.
    """
    seen: list[object] = []

    async def fake_chat(model_config, system, prompt, *, max_tokens):
        seen.append(model_config)
        return ""

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    loop = _loop(tmp_path)
    loop.context = _context(tmp_path)
    loop.memory = _memory(tmp_path)
    before_summarizer, before_extractor = loop.context.summarizer, loop.memory.extractor

    loop.set_model("beta")

    assert loop.context.summarizer is not before_summarizer
    assert loop.memory.extractor is not before_extractor

    await loop.context.summarizer("历史")
    await loop.memory.extractor([], None)

    assert seen, "neither helper reached _chat"
    for model_config in seen:
        assert getattr(model_config, "name", "") == "beta-v1"


# ---- 目录与退役 -------------------------------------------------------------


def test_add_model_refuses_to_overwrite(tmp_path):
    loop = _loop(tmp_path)

    with pytest.raises(ValueError):
        loop.add_model("alpha", _models()["beta"])


def test_add_model_makes_a_model_switchable(tmp_path):
    loop = _loop(tmp_path)
    extra = ModelConfig(name="extra-v1", base_url="https://x.test/v1", api_key="kx")

    loop.add_model("extra", extra)
    loop.set_model("extra")

    assert loop.models["extra"] is extra
    assert loop.engine.model_config.name == "extra-v1"


@pytest.mark.asyncio
async def test_closing_the_loop_closes_the_retired_engines(tmp_path):
    old = _Recorder(_models()["alpha"])
    loop = _loop(tmp_path, engine=old)

    loop.set_model("beta")
    assert old.closed is False, "no running loop: the close is deferred"

    await loop.aclose()

    assert old.closed is True
