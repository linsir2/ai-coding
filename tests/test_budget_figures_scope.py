"""F5: the sidebar's money line must say *what* it is and *how* cache is priced.

Trace 2026-10-31 (session f1d881ee): the card said "4 轮 · 20 条消息" beside
"¥75.64 · 缓存 72%".  Both numbers were right and neither was the one a reader
assumed: the cost is the whole run (117 model steps, plan waves and sub-agents
included, because ephemeral step sessions keep the parent's trace id), and the
cache hits were charged at the *input* price because no cached rate was
configured.  The card now says 累计, names the step count when it exceeds the
session's own turns, and says when cache hits are charged at input.
"""

from ai_coding.app.trace_text import budget_figures
from ai_coding.core.budget import Pricing


def _summary(**over) -> dict:
    base = {
        "turns": 4,
        "steps": 117,
        "prompt_tokens": 11_495_526,
        "completion_tokens": 120_887,
        "cached_tokens": 8_256_512,
        "by_model": {
            "qwen3.7-flash": {
                "prompt_tokens": 11_495_526,
                "completion_tokens": 120_887,
                "cached_tokens": 8_256_512,
                "steps": 117,
            }
        },
    }
    base.update(over)
    return base


def test_the_figures_carry_the_step_count_and_the_cache_price_verdict():
    figures = budget_figures(
        _summary(),
        prices={"qwen3.7-flash": Pricing(input_per_1k=0.001, output_per_1k=0.002)},
    )

    assert figures["steps"] == 117
    assert figures["cachedPriceKnown"] is False, (
        "cached tokens were charged at the input price; the card must say so"
    )
    assert figures["cachedPercent"] == 72


def test_a_configured_cache_rate_makes_the_cost_a_real_bill():
    figures = budget_figures(
        _summary(),
        prices={
            "qwen3.7-flash": Pricing(
                input_per_1k=0.001, output_per_1k=0.002, cached_per_1k=0.0001
            )
        },
    )

    assert figures["cachedPriceKnown"] is True


def test_without_cached_tokens_there_is_nothing_to_warn_about():
    figures = budget_figures(
        _summary(
            cached_tokens=0,
            by_model={
                "qwen3.7-flash": {
                    "prompt_tokens": 11_495_526,
                    "completion_tokens": 120_887,
                    "cached_tokens": 0,
                    "steps": 117,
                }
            },
        ),
        prices={"qwen3.7-flash": Pricing(input_per_1k=0.001, output_per_1k=0.002)},
    )

    assert figures["cachedPriceKnown"] is True


def test_an_unknown_price_still_hides_the_cost():
    figures = budget_figures(_summary(), prices={})

    assert figures["cost"] is None
    assert figures["steps"] == 117
