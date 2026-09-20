"""TDD (M1-1): service factory from AppConfig."""

import pytest

from ai_coding.ai.agents_sdk_service import AgentsSDKChatService
from ai_coding.ai.factory import build_service_from_config
from ai_coding.config.models import AppConfig
from ai_coding.domain.model_config import ModelConfig


def test_factory_default_model():
    cfg = AppConfig(
        default_model="deepseek-chat",
        models={"deepseek-chat": ModelConfig(name="deepseek-chat", base_url="u", api_key="k")},
    )
    svc = build_service_from_config(cfg)
    assert isinstance(svc, AgentsSDKChatService)


def test_factory_no_default_model_raises():
    cfg = AppConfig(default_model="", models={})
    with pytest.raises(ValueError, match="no default model"):
        build_service_from_config(cfg)


def test_factory_default_model_not_in_models_raises():
    other = ModelConfig(name="x", base_url="u", api_key="k")
    cfg = AppConfig(default_model="ghost", models={"other": other})
    with pytest.raises(ValueError, match="not found"):
        build_service_from_config(cfg)
