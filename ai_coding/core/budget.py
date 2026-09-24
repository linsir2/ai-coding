"""Token accounting for one turn (R4a).

Two rules shape this module:

* **Numbers come from the endpoint, not from us.**  ``prompt_tokens``,
  ``completion_tokens`` and the provider's own ``cached_tokens`` are recorded
  verbatim; our character-based estimate is kept *beside* them so the gap between
  the two is visible rather than hidden (docs/r4-analysis.md 4.3/4.4).
* **A missing number stays missing.**  An endpoint that reports no cache figure
  yields ``None``, never ``0`` — a zero would look like "no cache" and quietly
  mislead every downstream report.

Pure, synchronous and IO-free, so guards and the turn policy can read it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ai_coding.domain.usage import as_int as _as_int
from ai_coding.domain.usage import cache_from_usage

__all__ = [
    "BudgetLedger",
    "Pricing",
    "UsageTotals",
    "cache_from_usage",
]


@dataclass(frozen=True)
class Pricing:
    """Per-1k-token prices; both zero means "not configured" (cost is unknown)."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0

    @property
    def configured(self) -> bool:
        return self.input_per_1k > 0 or self.output_per_1k > 0


@dataclass(frozen=True)
class UsageTotals:
    """What one turn spent, summed over its model steps."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    steps: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BudgetLedger:
    """A turn's ledger: pure, synchronous, IO-free."""

    def __init__(self) -> None:
        self._prompt = 0
        self._completion = 0
        self._cached = 0
        self._cached_reported = False
        self._steps = 0
        self._latency_ms = 0
        self._estimated_tokens = 0
        self._prefix_ratio = 0.0

    # ---- recording ---------------------------------------------------------

    def record_step(
        self,
        usage: Mapping[str, Any] | None,
        *,
        cached: int | None = None,
        cache_write: int | None = None,
        latency_ms: int = 0,
    ) -> None:
        """Record one model step.  Absent figures are simply not added."""
        del cache_write  # recorded by the caller's event; the ledger sums reads
        self._steps += 1
        self._latency_ms += int(latency_ms or 0)
        if not isinstance(usage, Mapping):
            derived, _ = cache_from_usage(None)
            cached = cached if cached is not None else derived
            if cached is not None:
                self._cached += int(cached)
                self._cached_reported = True
            return
        self._prompt += int(_as_int(usage.get("prompt_tokens")) or 0)
        self._completion += int(_as_int(usage.get("completion_tokens")) or 0)
        derived_cached, _ = cache_from_usage(usage)
        effective = cached if cached is not None else derived_cached
        if effective is not None:
            self._cached += int(effective)
            self._cached_reported = True

    def record_assembly(self, estimated_tokens: int, prefix_ratio: float) -> None:
        """Record what this request *looked* like before the call."""
        self._estimated_tokens = int(estimated_tokens)
        self._prefix_ratio = float(prefix_ratio)

    # ---- reading -----------------------------------------------------------

    @property
    def totals(self) -> UsageTotals:
        return UsageTotals(
            prompt_tokens=self._prompt,
            completion_tokens=self._completion,
            cached_tokens=self._cached,
            steps=self._steps,
        )

    @property
    def latency_ms(self) -> int:
        return self._latency_ms

    @property
    def estimated_tokens(self) -> int:
        return self._estimated_tokens

    @property
    def prefix_shared_ratio(self) -> float:
        return self._prefix_ratio

    @property
    def calibration(self) -> float | None:
        """Reported prompt tokens divided by our estimate (``None`` if either is 0)."""
        if self._estimated_tokens <= 0 or self._prompt <= 0:
            return None
        return self._prompt / self._estimated_tokens

    @property
    def cache_hit_ratio(self) -> float | None:
        """``cached_tokens / prompt_tokens`` — ``None`` when nobody reported it."""
        if not self._cached_reported or self._prompt <= 0:
            return None
        return self._cached / self._prompt

    def cost(self, pricing: Pricing | None) -> float | None:
        """Money spent, or ``None`` when no price is configured."""
        if pricing is None or not pricing.configured:
            return None
        return (
            self._prompt / 1000 * pricing.input_per_1k
            + self._completion / 1000 * pricing.output_per_1k
        )
