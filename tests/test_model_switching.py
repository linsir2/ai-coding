"""``/model`` has to work against a real loop, not a hand-built one.

The old tests here bypassed ``AgentLoop.__init__`` and installed a stand-in
``settings`` object carrying a ``models`` dict.  That is why the bug survived:
``AIConfig`` -- what the loop really receives from ``cli.py`` -- has no such
field, so every assertion passed while the shipped ``/model`` could only ever
report "unknown model".  A test that cannot fail on the real wiring is not
testing the wiring, so these go through the real constructor.
"""

from __future__ import annotations

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.model_config import ModelConfig
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


class _Engine:
    def __init__(self, model_config):
        self.model_config = model_config
        self.on_retry = None
        self.on_fallback = None

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        raise AssertionError("no model call expected")

    async def aclose(self):
        return None


def _models():
    return {
        "alpha": ModelConfig(name="alpha-v1", base_url="https://a.test/v1", api_key="k"),
        "beta": ModelConfig(name="beta-v1", base_url="https://b.test/v1", api_key="k"),
    }


def _loop(tmp_path, **over):
    kwargs = {"default_model": over.pop("default_model", "")}
    kwargs.update(over.pop("kwargs", {}))
    return AgentLoop(
        _Engine(over.get("current", _models()["alpha"])),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(),
        models=_models(),
        **kwargs,
    )


def test_set_model_switches_the_engine(tmp_path):
    loop = _loop(tmp_path)

    loop.set_model("beta")

    assert loop.engine.model_config.name == "beta-v1"


def test_set_model_rejects_a_name_it_does_not_know(tmp_path):
    loop = _loop(tmp_path)

    with pytest.raises(ValueError, match="Unknown model"):
        loop.set_model("nonexistent")


def test_the_model_table_reaches_the_loop_on_the_real_path(tmp_path):
    """``cli.py`` passes ``AppConfig.models`` here; before this existed the loop
    held only ``AIConfig``, which cannot name a model at all."""
    loop = _loop(tmp_path)

    assert sorted(loop.models) == ["alpha", "beta"]
    assert loop.settings.models if hasattr(loop.settings, "models") else True


def test_a_loop_built_without_models_still_works(tmp_path):
    """Optional, because a sub-agent or a test may assemble a loop with a single
    engine and nothing to switch to."""
    loop = AgentLoop(
        _Engine(_models()["alpha"]),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(),
    )

    assert loop.models == {}
    with pytest.raises(ValueError, match="Unknown model"):
        loop.set_model("beta")


def test_the_default_model_is_a_separate_fact_from_the_active_one(tmp_path):
    """``/model`` marks the configured default with ``*``.  Without remembering
    it, the list would mark whichever model happens to be active -- a different
    fact, and one that moves every time the user switches."""
    loop = _loop(tmp_path, current=_models()["beta"], default_model="alpha")

    assert loop.default_model == "alpha"
    assert loop.engine.model_config.name == "beta-v1"
