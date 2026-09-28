"""Footer: model info + context + timer + exit hint."""

import time

from ai_coding.ui.footer import render_footer


class _FakeModelConfig:
    name = "test-model"


class _FakeEngine:
    model_config = _FakeModelConfig()


class _FakeSettings:
    max_context_tokens = 1000


class _FakeUsage:
    total_tokens = 500


class _FakeOutcome:
    usage = _FakeUsage()


class _FakeLoop:
    engine = _FakeEngine()
    settings = _FakeSettings()
    last_outcome = _FakeOutcome()


def test_renders_model_name():
    text = "".join(t for _, t in render_footer(_FakeLoop(), time.monotonic()))
    assert "test-model" in text


def test_renders_context_percentage():
    text = "".join(t for _, t in render_footer(_FakeLoop(), time.monotonic()))
    assert "50%" in text


def test_renders_timer():
    text = "".join(t for _, t in render_footer(_FakeLoop(), time.monotonic() - 65))
    assert "01:05" in text


def test_renders_exit_hint():
    text = "".join(t for _, t in render_footer(_FakeLoop(), time.monotonic()))
    assert "ctrl+c" in text


def test_renders_commands_hint():
    text = "".join(t for _, t in render_footer(_FakeLoop(), time.monotonic()))
    assert "/" in text
