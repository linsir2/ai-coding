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


# ---- cache visibility (R11 review: usage_dict dropped what the ledger kept) ----

def test_turn_usage_carries_cached_tokens():
    """The ledger has always counted cache hits; usage_dict must hand them over.

    Measured on the real loop before this: every provider call returned
    ``cached_tokens: 1024`` while ``loop.last_outcome.usage`` reported 0, so
    receipts, the persisted per-turn usage and /budget all treated the cache as
    free.  The number existed one call away.
    """
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 100, "completion_tokens": 10,
                        "total_tokens": 110, "cached_tokens": 40})
    ledger.record_step({"prompt_tokens": 200, "completion_tokens": 20,
                        "total_tokens": 220, "cached_tokens": 120})

    used = ledger.usage_dict()

    assert used["prompt_tokens"] == 300
    assert used["cached_tokens"] == 160
    assert ledger.cache_hit_ratio == 160 / 300


def test_cache_is_omitted_rather_than_reported_as_zero():
    """No provider numbers means no key — a zero would be a claim, not a fact."""
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 100, "completion_tokens": 10})

    assert "cached_tokens" not in ledger.usage_dict()
    assert ledger.cache_hit_ratio is None


def test_cache_writes_stay_on_the_event_and_out_of_the_ledger():
    """R4a decided this: the ledger sums reads, the event carries the write.

    ``record_step`` drops ``cache_write`` on purpose ("recorded by the caller's
    event"), and ``ModelStepFinished.cache_write_tokens`` is where a provider's
    write figure lands.  My first pass at usage_dict tried to add
    ``cache_write_tokens`` and would have quietly reversed that.
    """
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 100, "completion_tokens": 10,
                        "cached_tokens": 30, "cache_write_tokens": 64})

    used = ledger.usage_dict()
    assert used["cached_tokens"] == 30
    assert "cache_write_tokens" not in used


# ---- cache pricing ---------------------------------------------------------

def test_cost_charges_cached_tokens_at_their_own_price():
    """Cached input is cheaper everywhere; charging it as fresh input overstates.

    Both configured providers sell cache hits at a fraction of the input price
    (DeepSeek roughly a tenth), so a turn that is two thirds cached is billed
    here at the full input rate — the number was never shown to anyone, because
    nothing called ``cost()``; it is worth having correct before something does.
    """
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 1000, "completion_tokens": 500,
                        "total_tokens": 1500, "cached_tokens": 400})
    price = Pricing(input_per_1k=0.01, output_per_1k=0.03, cached_per_1k=0.001)

    # 600 fresh + 400 cached input, 500 output
    assert ledger.cost(price) == 0.600 * 0.01 + 0.400 * 0.001 + 0.500 * 0.03


def test_cost_falls_back_to_the_input_price_when_no_cache_price_is_configured():
    """An unconfigured cache price must not silently make cached tokens free."""
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 1000, "completion_tokens": 500,
                        "total_tokens": 1500, "cached_tokens": 400})
    price = Pricing(input_per_1k=0.01, output_per_1k=0.03)

    assert ledger.cost(price) == 1.000 * 0.01 + 0.500 * 0.03


# ---- G7: spend attributed to the model that spent it ------------------------


def test_usage_is_bucketed_by_the_model_that_spent_it():
    """A fallback chain routes one turn through two providers, and "did that
    cost more or less" cannot be answered from a single total."""
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 1_000, "completion_tokens": 100}, model="qwen")
    ledger.record_step({"prompt_tokens": 2_000, "completion_tokens": 200}, model="deepseek")
    ledger.record_step({"prompt_tokens": 500, "completion_tokens": 50}, model="qwen")

    by_model = ledger.by_model()

    assert by_model["qwen"]["prompt_tokens"] == 1_500
    assert by_model["qwen"]["completion_tokens"] == 150
    assert by_model["qwen"]["steps"] == 2
    assert by_model["deepseek"]["prompt_tokens"] == 2_000
    assert by_model["deepseek"]["steps"] == 1


def test_the_totals_still_include_every_model():
    """Bucketing is an attribution, not a replacement: the session total is
    the same number it was before."""
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 1_000, "completion_tokens": 100}, model="qwen")
    ledger.record_step({"prompt_tokens": 2_000, "completion_tokens": 200}, model="deepseek")

    assert ledger.totals.prompt_tokens == 3_000
    assert ledger.totals.completion_tokens == 300


def test_a_step_without_a_model_is_counted_but_not_attributed():
    """No model means nobody could say which one -- a bucket named "unknown"
    would be a claim the ledger cannot support."""
    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 1_000, "completion_tokens": 100})

    assert ledger.totals.prompt_tokens == 1_000
    assert ledger.by_model() == {}


def test_cached_tokens_are_bucketed_too():
    ledger = BudgetLedger()
    ledger.record_step(
        {"prompt_tokens": 1_000, "completion_tokens": 10, "cached_tokens": 600},
        model="qwen",
    )

    assert ledger.by_model()["qwen"]["cached_tokens"] == 600


def test_an_empty_ledger_has_no_models():
    assert BudgetLedger().by_model() == {}
