"""F5: a provider publishes per-1M prices; the config's historical key is per-1k.

Pasting "¥2 / M tokens" into ``price_input_per_1k`` charges 1000x.  The alias
``price_input_per_1m`` is converted at load time, so a real bill and a
1000x-inflated one cannot both be produced from the same published number.
"""

from ai_coding.config.models import _coerce_model


def _raw(**over):
    base = {
        "name": "qwen",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
    }
    base.update(over)
    return base


def test_a_per_million_price_is_converted_to_per_thousand():
    config = _coerce_model(_raw(price_input_per_1m=2.0))  # ¥2 / M

    assert config.price_input_per_1k == 0.002


def test_the_legacy_per_thousand_key_still_works():
    config = _coerce_model(_raw(price_input_per_1k=0.002))

    assert config.price_input_per_1k == 0.002


def test_the_per_million_key_wins_when_both_are_present():
    config = _coerce_model(
        _raw(price_input_per_1k=2.0, price_input_per_1m=2.0)
    )

    assert config.price_input_per_1k == 0.002
