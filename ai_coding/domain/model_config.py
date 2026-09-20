"""Model connection configuration (corrected key ``base_url`` from Java ``baseURL``)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelConfig:
    name: str
    base_url: str
    api_key: str
    streaming: bool = True
    max_tokens: int = 4096
    temperature: float = 0.7
    top_p: float = 0.9
    timeout: int = 60
