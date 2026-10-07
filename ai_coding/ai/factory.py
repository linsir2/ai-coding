"""Engine factory: AppConfig -> ChatEngine (the single engine wiring point)."""

from __future__ import annotations

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AppConfig
from ai_coding.domain.model_config import ModelConfig

__all__ = ["build_engine_from_config"]


def _resolve_chain(models: dict[str, ModelConfig], start: str) -> list[str]:
    """Walk ``fallback_models`` from ``start`` until it stops.

    Eager, so a chain that cannot work -- a name that is not in ``models``, or a
    loop -- is refused while the process is starting and the error can still say
    which model closed the loop.  Waiting for the first outage to discover it
    would turn a typo in a config file into a failure that looks like a provider
    outage.
    """
    order: list[str] = []
    seen: set[str] = set()
    current: str | None = start
    while current is not None:
        if current in seen:
            raise ValueError(
                f"fallback_models for {start!r} contains a cycle at {current!r}"
            )
        if current not in models:
            raise ValueError(
                f"fallback model {current!r} for {start!r} not found in models "
                f"(known: {', '.join(sorted(models)) or 'none'})"
            )
        seen.add(current)
        order.append(current)
        current = next(iter(models[current].fallback_models), None)
    return order


def build_engine_from_config(app_config: AppConfig) -> ChatEngine:
    """Resolve ``default_model`` into a step-wise ``ChatEngine``.

    A model with no ``fallback_models`` yields a bare ``OpenAIChatEngine``, which
    is what every config written before M2 produces -- the chain only exists when
    a config asks for one.

    Raises:
        ValueError: no default model, the default is absent from ``models``, or
            the declared chain names an unknown model or loops back on itself.
            Never touches the network.
    """
    default = app_config.default_model
    if not default:
        raise ValueError("no default model configured")

    order = _resolve_chain(app_config.models, default)

    from ai_coding.ai.fallback_engine import FallbackEngine
    from ai_coding.ai.openai_engine import OpenAIChatEngine

    stream_usage = bool(getattr(app_config.ai, "stream_usage", True))
    engines = [OpenAIChatEngine(app_config.models[key], stream_usage=stream_usage) for key in order]
    if len(engines) == 1:
        return engines[0]
    return FallbackEngine(engines)
