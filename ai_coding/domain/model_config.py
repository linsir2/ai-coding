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
    # R4a: optional prices, used only to turn token counts into money.  Zero means
    # "unknown", and an unknown price must yield no cost rather than a fake 0.0
    # (docs/r4-analysis.md 4.3).
    price_input_per_1k: float = 0.0
    price_output_per_1k: float = 0.0
