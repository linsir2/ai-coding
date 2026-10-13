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
    # P0-1: bounded retries for a *transient* provider failure.  Zero restores
    # the pre-P0-1 behaviour of one attempt and one failure.  The classification
    # lives in ``ai/openai_engine.py``; only the budget is configuration.
    max_retries: int = 3
    retry_base_seconds: float = 2.0
    # R4a: optional prices, used only to turn token counts into money.  Zero means
    # "unknown", and an unknown price must yield no cost rather than a fake 0.0
    # (docs/r4-analysis.md 4.3).
    price_input_per_1k: float = 0.0
    price_output_per_1k: float = 0.0
    # R11: cache hits are billed at their own rate (DeepSeek roughly a tenth of
    # input).  Zero means "unknown" and the cost then charges those tokens at
    # the input price — never free.
    price_cached_per_1k: float = 0.0
    # M2: models to try, in order, when this one cannot answer.  Keys of
    # ``AppConfig.models`` -- not ``name``, which may differ (``qwen`` ->
    # ``qwen3.8-2.4t-a95b``).  Empty means "no chain", which is what every
    # config written before M2 means, so the factory can short-circuit on it and
    # behave exactly as before.
    fallback_models: tuple[str, ...] = ()
    # The model's own context window, in tokens.  0 means "not declared", which
    # is not the same as any number: a model that has not said keeps the
    # conservative default instead of being trusted with an invented one.
    context_window: int = 0
