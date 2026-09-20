"""Service factory: AppConfig -> AIService instance (single engine wiring point)."""

from __future__ import annotations

from ai_coding.ai.agents_sdk_service import AgentsSDKChatService
from ai_coding.ai.base import AIService
from ai_coding.config.models import AppConfig


def build_service_from_config(app_config: AppConfig) -> AIService:
    """Resolve ``default_model`` into a concrete ``AIService``.

    Raises ``ValueError`` when no default model is configured or the named model
    is absent from ``models``. Never touches the network.
    """
    default = app_config.default_model
    if not default:
        raise ValueError("no default model configured")
    model = app_config.models.get(default)
    if model is None:
        raise ValueError(f"default model '{default}' not found in models")
    return AgentsSDKChatService(model_config=model)


__all__ = ["build_service_from_config"]
