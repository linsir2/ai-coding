"""AI engine package: port, mapping, SDK adapter, and factory."""

from ai_coding.ai.base import AIService
from ai_coding.ai.factory import build_service_from_config

__all__ = ["AIService", "build_service_from_config"]
