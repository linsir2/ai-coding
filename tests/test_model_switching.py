"""AgentLoop.set_model: switch model mid-session."""

import pytest

from ai_coding.core.agent_loop import AgentLoop


def test_set_model_exists():
    assert hasattr(AgentLoop, "set_model")
    assert callable(getattr(AgentLoop, "set_model", None))


def test_set_model_rejects_unknown():
    loop = AgentLoop.__new__(AgentLoop)
    loop.settings = type("S", (), {"models": {}})()
    loop.engine = type("E", (), {"model_config": None})()
    with pytest.raises(ValueError, match="Unknown model"):
        loop.set_model("nonexistent")


def test_set_model_updates_engine():
    new_cfg = type("MC", (), {"name": "new-model"})()
    loop = AgentLoop.__new__(AgentLoop)
    loop.settings = type("S", (), {"models": {"new": new_cfg}})()
    loop.engine = type("E", (), {"model_config": None})()
    loop.set_model("new")
    assert loop.engine.model_config is new_cfg
