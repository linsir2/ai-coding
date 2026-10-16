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
    """Per-1k-token prices; both zero means "not configured" (cost is unknown).

    ``cached_per_1k`` is the price of a prefix the provider already has.  Both
    configured providers sell it at a fraction of the input price (DeepSeek
    roughly a tenth), so charging cache hits as fresh input overstates a turn
    that is two thirds cached.  Left at 0 it means "no cache price known", and
    ``cost`` then charges those tokens at the input price — never free.
    """

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0
    cached_per_1k: float = 0.0

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
        self._by_model: dict[str, dict[str, int]] = {}
        self._prompt = 0
        self._completion = 0
        #: 19 §3.5: the window's occupancy — the LAST reporting step's prompt
        #: tokens, overwritten per step (not summed like the totals above).
        self._context = 0
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
        model: str = "",
        cached: int | None = None,
        cache_write: int | None = None,
        latency_ms: int = 0,
    ) -> None:
        """Record one model step.  Absent figures are simply not added.

        ``model`` attributes the step to whoever answered it (G7).  Empty means
        nobody could say, and the step is then counted in the totals but in no
        bucket -- a bucket named "unknown" would be a claim the ledger cannot
        support, and it would hide the difference from a real model whose name
        happens to be missing.
        """
        del cache_write  # recorded by the caller's event; the ledger sums reads
        self._steps += 1
        self._latency_ms += int(latency_ms or 0)
        if model and isinstance(usage, Mapping):
            bucket = self._by_model.setdefault(
                model, {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "steps": 0}
            )
            bucket["steps"] += 1
            bucket["prompt_tokens"] += int(_as_int(usage.get("prompt_tokens")) or 0)
            bucket["completion_tokens"] += int(_as_int(usage.get("completion_tokens")) or 0)
            derived, _ = cache_from_usage(usage)
            effective = cached if cached is not None else derived
            if effective is not None:
                bucket["cached_tokens"] += int(effective)
        if not isinstance(usage, Mapping):
            derived, _ = cache_from_usage(None)
            cached = cached if cached is not None else derived
            if cached is not None:
                self._cached += int(cached)
                self._cached_reported = True
            return
        self._prompt += int(_as_int(usage.get("prompt_tokens")) or 0)
        self._completion += int(_as_int(usage.get("completion_tokens")) or 0)
        # 19 §3.5: both readers of the context number (footer meter, compaction
        # gate) mean "how full is the window now" — that is this step's prompt,
        # not the turn's running total.  Last reporting step wins.
        self._context = int(_as_int(usage.get("prompt_tokens")) or 0)
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

    def by_model(self) -> dict[str, dict[str, int]]:
        """Token usage per model, for "did the fallback cost more or less".

        A copy, so a caller rendering it cannot rewrite what was recorded.
        """
        return {name: dict(spend) for name, spend in self._by_model.items()}

    def cost(self, pricing: Pricing | None) -> float | None:
        """Money spent, or ``None`` when no price is configured."""
        if pricing is None or not pricing.configured:
            return None
        cached = min(self._cached, self._prompt) if self._cached_reported else 0
        fresh = max(self._prompt - cached, 0)
        cached_rate = pricing.cached_per_1k or pricing.input_per_1k
        return (
            fresh / 1000 * pricing.input_per_1k
            + cached / 1000 * cached_rate
            + self._completion / 1000 * pricing.output_per_1k
        )

    def usage_dict(self) -> dict[str, int]:
        """The turn's totals in the same shape the engine reports per step.

        Including the cache figures, which the ledger has counted all along:
        measured on the real loop before this, every provider call reported
        ``cached_tokens: 1024`` while this dict said 0, so receipts, the
        persisted per-turn usage and /budget all treated the cache as free.
        Omitted rather than zeroed when nobody reported them — a zero would be
        a claim, and "no provider numbers" is a different fact from "no hits".
        """
        totals = self.totals
        out = {
            "prompt_tokens": totals.prompt_tokens,
            "completion_tokens": totals.completion_tokens,
            "total_tokens": totals.total_tokens,
        }
        if self._cached_reported:
            out["cached_tokens"] = totals.cached_tokens
        if self._context:
            out["context_tokens"] = self._context
        return out
