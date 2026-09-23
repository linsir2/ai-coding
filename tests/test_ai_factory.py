"""TDD: engine factory from AppConfig."""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.factory import build_engine_from_config
from ai_coding.ai.openai_engine import OpenAIChatEngine
from ai_coding.config.models import AIConfig, AppConfig
from ai_coding.domain.model_config import ModelConfig


def _cfg(default="m", **ai_over):
    return AppConfig(
        default_model=default,
        models={"m": ModelConfig(name="m", base_url="https://x/v1", api_key="k")},
        ai=AIConfig(**ai_over),
    )


def test_factory_builds_the_step_wise_engine():
    engine = build_engine_from_config(_cfg())
    assert isinstance(engine, OpenAIChatEngine)
    assert isinstance(engine, ChatEngine)


def test_factory_no_default_model_raises():
    with pytest.raises(ValueError, match="no default model"):
        build_engine_from_config(AppConfig(default_model="", models={}))


def test_factory_default_model_not_in_models_raises():
    other = ModelConfig(name="x", base_url="u", api_key="k")
    cfg = AppConfig(default_model="ghost", models={"other": other})
    with pytest.raises(ValueError, match="not found"):
        build_engine_from_config(cfg)


def test_factory_forwards_stream_usage_setting():
    engine = build_engine_from_config(_cfg(stream_usage=False))
    assert isinstance(engine, OpenAIChatEngine)
    assert engine._stream_usage is False
