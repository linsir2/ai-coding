"""The summary delta is an internal policy, not a user knob.

It used to be a flat 4000: 1.5% of a 256k window, 8% of a 48k one.  The rule now
scales with the window, with a floor so a tiny window still summarises:

    threshold = configured if configured > 0 else max(1000, window * 0.02)

0 (the new default) means "derive it"; a positive value stays an override.
"""

import tempfile
from pathlib import Path

from ai_coding.config.models import AIConfig
from ai_coding.core.context_compression import ContextManager


def _manager(window: int, override: int = 0) -> ContextManager:
    return ContextManager(
        AIConfig(**{"max_context_tokens": window, "summary_delta_tokens": override}),
        transcripts_root=Path(tempfile.mkdtemp()),
        summarizer=None,
    )


def test_the_threshold_scales_with_the_window():
    assert _manager(256_000)._summary_delta_tokens == 5_120  # 2%
    assert _manager(128_000)._summary_delta_tokens == 2_560


def test_a_small_window_keeps_a_floor():
    assert _manager(48_000)._summary_delta_tokens == 1_000  # max(1000, 960)
    assert _manager(6_000)._summary_delta_tokens == 1_000


def test_an_explicit_value_still_wins():
    assert _manager(256_000, override=4_000)._summary_delta_tokens == 4_000
    assert _manager(48_000, override=1)._summary_delta_tokens == 1


def test_a_model_switch_recomputes_the_threshold():
    manager = _manager(48_000)

    manager._max_context_tokens = 256_000

    assert manager._summary_delta_tokens == 5_120
    assert manager.max_context_tokens == 256_000


def test_the_config_default_means_derive():
    assert AIConfig().summary_delta_tokens == 0
