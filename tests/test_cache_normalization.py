"""TDD (R4a): reading prompt-cache numbers out of whatever the endpoint sends.

Cache accounting differs per vendor, and inventing a number is worse than
admitting there is none, so an unknown shape yields ``None`` rather than zero
(docs/r4-analysis.md 4.4).
"""

from ai_coding.core.budget import cache_from_usage


def test_openai_shape():
    usage = {"prompt_tokens": 900, "prompt_tokens_details": {"cached_tokens": 640}}
    assert cache_from_usage(usage) == (640, None)


def test_anthropic_shape_reports_both_read_and_write():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 90}
    assert cache_from_usage(usage) == (500, 90)


def test_deepseek_shape():
    usage = {"prompt_tokens": 800, "prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 200}
    assert cache_from_usage(usage) == (600, None)


def test_our_own_flat_shape_is_accepted():
    usage = {"prompt_tokens": 100, "cached_tokens": 40, "cache_write_tokens": 7}
    assert cache_from_usage(usage) == (40, 7)


def test_unknown_shapes_and_nulls_are_not_guessed():
    assert cache_from_usage({}) == (None, None)
    assert cache_from_usage(None) == (None, None)
    assert cache_from_usage({"prompt_tokens": 10}) == (None, None)
    # "null" must not blow up or turn into 0 (R2 already hit this in the engine)
    assert cache_from_usage({"cached_tokens": None}) == (None, None)
    assert cache_from_usage({"prompt_tokens_details": {"cached_tokens": None}}) == (None, None)
    assert cache_from_usage({"prompt_tokens_details": None}) == (None, None)
