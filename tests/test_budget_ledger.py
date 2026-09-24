"""TDD (R4a): the per-turn token ledger.

The ledger is the only place that knows what a turn cost.  It is deliberately
dumb and pure: it records, it adds up, and it refuses to invent numbers it does
not have (docs/r4-analysis.md 4.3).
"""

from ai_coding.core.budget import BudgetLedger, Pricing, UsageTotals


def test_totals_accumulate_across_steps():
    ledger = BudgetLedger()
    ledger.record_step(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        cached=80,
        cache_write=None,
        latency_ms=1000,
    )
    ledger.record_step(
        {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
        cached=250,
        cache_write=None,
        latency_ms=2000,
    )

    totals = ledger.totals
    assert isinstance(totals, UsageTotals)
    assert totals.prompt_tokens == 400
    assert totals.completion_tokens == 60
    assert totals.total_tokens == 460
    assert totals.cached_tokens == 330
    assert totals.steps == 2
    assert ledger.latency_ms == 3000


def test_missing_usage_is_ignored_but_counted_as_a_step():
    ledger = BudgetLedger()
    ledger.record_step(None, cached=None, cache_write=None, latency_ms=50)

    assert ledger.totals.steps == 1
    assert ledger.totals.total_tokens == 0
    assert ledger.cache_hit_ratio is None
    assert ledger.calibration is None


def test_calibration_compares_reported_prompt_tokens_with_our_estimate():
    ledger = BudgetLedger()
    ledger.record_assembly(estimated_tokens=1000, prefix_ratio=0.9)
    ledger.record_step(
        {"prompt_tokens": 1120, "completion_tokens": 0, "total_tokens": 1120},
        cached=None,
        cache_write=None,
        latency_ms=1,
    )

    assert ledger.calibration is not None
    assert abs(ledger.calibration - 1.12) < 1e-9
    assert ledger.prefix_shared_ratio == 0.9


def test_cache_hit_ratio_uses_the_provider_number():
    ledger = BudgetLedger()
    ledger.record_step(
        {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010},
        cached=750,
        cache_write=None,
        latency_ms=1,
    )
    assert ledger.cache_hit_ratio == 0.75


def test_cost_uses_the_model_price_but_stays_none_when_unknown():
    ledger = BudgetLedger()
    ledger.record_step(
        {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        cached=None,
        cache_write=None,
        latency_ms=1,
    )

    assert ledger.cost(None) is None
    assert ledger.cost(Pricing()) is None  # a zero price means "not configured"
    price = Pricing(input_per_1k=0.01, output_per_1k=0.03)
    assert ledger.cost(price) == 1000 / 1000 * 0.01 + 500 / 1000 * 0.03


def test_record_assembly_tracks_the_last_prefix_ratio():
    ledger = BudgetLedger()
    ledger.record_assembly(estimated_tokens=10, prefix_ratio=0.1)
    ledger.record_assembly(estimated_tokens=20, prefix_ratio=0.95)
    assert ledger.prefix_shared_ratio == 0.95
    assert ledger.estimated_tokens == 20
