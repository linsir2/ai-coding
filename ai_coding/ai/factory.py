"""Engine factory: AppConfig -> ChatEngine (the single engine wiring point)."""

from __future__ import annotations

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AppConfig


def build_engine_from_config(app_config: AppConfig) -> ChatEngine:
    """Resolve ``default_model`` into a step-wise ``ChatEngine``.

    Raises ``ValueError`` when no default model is configured or the named model
    is absent from ``models``.  Never touches the network.
    """
    default = app_config.default_model
    if not default:
        raise ValueError("no default model configured")
    model = app_config.models.get(default)
    if model is None:
        raise ValueError(f"default model '{default}' not found in models")

    from ai_coding.ai.openai_engine import OpenAIChatEngine

    stream_usage = bool(getattr(app_config.ai, "stream_usage", True))
    return OpenAIChatEngine(model, stream_usage=stream_usage)


__all__ = ["build_engine_from_config"]
