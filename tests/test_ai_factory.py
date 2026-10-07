"""TDD: engine factory from AppConfig."""

from dataclasses import replace

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.factory import build_engine_from_config
from ai_coding.ai.fallback_engine import FallbackEngine
from ai_coding.ai.openai_engine import OpenAIChatEngine
from ai_coding.config.models import AIConfig, AppConfig
from ai_coding.domain.model_config import ModelConfig


def _cfg(default="m", **ai_over):
    return AppConfig(
        default_model=default,
        models={"m": ModelConfig(name="m", base_url="https://x/v1", api_key="k")},
        ai=AIConfig(**ai_over),
    )


def test_factory_builds_the_step_wise_engine():
    engine = build_engine_from_config(_cfg())
    assert isinstance(engine, OpenAIChatEngine)
    assert isinstance(engine, ChatEngine)


def test_factory_no_default_model_raises():
    with pytest.raises(ValueError, match="no default model"):
        build_engine_from_config(AppConfig(default_model="", models={}))


def test_factory_default_model_not_in_models_raises():
    other = ModelConfig(name="x", base_url="u", api_key="k")
    cfg = AppConfig(default_model="ghost", models={"other": other})
    with pytest.raises(ValueError, match="not found"):
        build_engine_from_config(cfg)


def test_factory_forwards_stream_usage_setting():
    engine = build_engine_from_config(_cfg(stream_usage=False))
    assert isinstance(engine, OpenAIChatEngine)
    assert engine._stream_usage is False


# ---- M2: chain resolution happens here, once, at startup -------------------


def _two_models(primary_fallbacks=()):
    return AppConfig(
        default_model="primary",
        models={
            "primary": ModelConfig(
                name="primary",
                base_url="https://a.test/v1",
                api_key="k",
                fallback_models=tuple(primary_fallbacks),
            ),
            "backup": ModelConfig(name="backup", base_url="https://b.test/v1", api_key="k"),
        },
    )


def test_a_model_without_fallbacks_stays_a_plain_engine():
    """Every config written before M2 lands here, so this path must return the
    exact class it always returned -- no wrapper, no behaviour difference."""
    engine = build_engine_from_config(_two_models())

    assert isinstance(engine, OpenAIChatEngine)
    assert not isinstance(engine, FallbackEngine)


def test_a_declared_fallback_becomes_a_chain():
    engine = build_engine_from_config(_two_models(["backup"]))

    assert isinstance(engine, FallbackEngine)
    assert [e.model_config.name for e in engine._engines] == ["primary", "backup"]


def test_the_chain_follows_fallbacks_transitively():
    """``primary -> backup -> tertiary`` is three engines, not two: a backup that
    names its own backup is telling the truth about what it will do."""
    third = ModelConfig(name="tertiary", base_url="https://c.test/v1", api_key="k")
    cfg = _two_models(["backup"])
    cfg.models["backup"] = replace(cfg.models["backup"], fallback_models=("tertiary",))
    cfg.models["tertiary"] = third

    engine = build_engine_from_config(cfg)

    assert [e.model_config.name for e in engine._engines] == [
        "primary",
        "backup",
        "tertiary",
    ]


def test_a_fallback_that_does_not_exist_is_refused_at_startup():
    """A typo in the chain must not wait for the first outage to surface."""
    with pytest.raises(ValueError, match="ghost"):
        build_engine_from_config(_two_models(["ghost"]))


def test_a_cycle_is_refused_at_startup():
    """``a -> b -> a`` resolved eagerly would loop forever.  Caught while the
    chain is being built, where the message can still name the model that
    closed the loop."""
    cfg = _two_models(["backup"])
    cfg.models["backup"] = replace(cfg.models["backup"], fallback_models=("primary",))

    with pytest.raises(ValueError, match="cycle"):
        build_engine_from_config(cfg)


def test_a_model_naming_itself_is_a_cycle():
    cfg = _two_models(["primary"])

    with pytest.raises(ValueError, match="cycle"):
        build_engine_from_config(cfg)


def test_chain_members_keep_their_own_endpoint():
    """Each engine builds its own client from its own ``base_url``; sharing one
    would send the backup's traffic to the primary's host with the primary's
    key, which is the defect this composition exists to avoid."""
    engine = build_engine_from_config(_two_models(["backup"]))

    assert [e.model_config.base_url for e in engine._engines] == [
        "https://a.test/v1",
        "https://b.test/v1",
    ]
    assert [e._client.base_url for e in engine._engines] == [
        "https://a.test/v1/",
        "https://b.test/v1/",
    ]
