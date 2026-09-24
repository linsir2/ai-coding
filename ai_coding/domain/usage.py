"""Usage payload normalisation (R4a).

Endpoints disagree about how they report prompt-cache tokens, and this module is
the single place that knows the shapes.  It lives in ``domain`` because both the
engine (``ai``, which may only depend on ``domain``) and the accounting layer
(``core``) need it — duplicating the table would let the two drift apart.

The rule is simple: **a number we do not recognise is not a number.**  Unknown
shapes return ``None`` rather than ``0``, because a zero reads as "no cache use"
and would silently mislead every report built on top (docs/r4-analysis.md 4.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["as_int", "cache_from_usage"]


def as_int(value: Any) -> int | None:
    """Best-effort int; ``None``/booleans/garbage count as absent."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nested(usage: Mapping[str, Any], key: str, inner: str) -> Any:
    detail = usage.get(key)
    if isinstance(detail, Mapping):
        return detail.get(inner)
    return None


def cache_from_usage(
    usage: Mapping[str, Any] | None,
) -> tuple[int | None, int | None]:
    """Return ``(cached_tokens, cache_write_tokens)``, or ``(None, None)``.

    Known shapes:

    * ours / flat — ``cached_tokens``, ``cache_write_tokens``
    * OpenAI — ``prompt_tokens_details.cached_tokens``
    * Anthropic — ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
    * DeepSeek — ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
    """
    if not isinstance(usage, Mapping):
        return None, None

    cached = as_int(usage.get("cached_tokens"))
    if cached is None:
        cached = as_int(usage.get("cache_read_input_tokens"))
    if cached is None:
        cached = as_int(usage.get("prompt_cache_hit_tokens"))
    if cached is None:
        cached = as_int(_nested(usage, "prompt_tokens_details", "cached_tokens"))
    if cached is None:
        cached = as_int(_nested(usage, "input_tokens_details", "cached_tokens"))

    written = as_int(usage.get("cache_write_tokens"))
    if written is None:
        written = as_int(usage.get("cache_creation_input_tokens"))

    return cached, written
