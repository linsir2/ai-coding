"""I1 的两个"跟随"修复：子智能体与上下文窗口都要跟着 `/model` 走。

两处缺陷同源：某个运行期事实只在**一个**地方更新，别处还读着构造期/配置期的
旧值。

* 子智能体的 engine 是每次起它时新建的，但工厂读的是**配置默认**
  （``app_cfg.default_model``）——切到另一个 provider 之后，被派出去的活儿
  仍然打到旧端点；
* ``ContextManager`` 的 L3 阈值是窗口的一半，在构造期算一次——切到窗口更小的
  模型之后，阈值还是旧模型的，可能越过新窗口。

这里断言的是**行为**（子智能体真的换了端点、阈值真的跟着窗口走），不是
"某个字段被赋值了"。
"""

from __future__ import annotations

from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.context_compression import ContextManager
from ai_coding.domain.model_config import ModelConfig
from ai_coding.service.session_service import SessionService
from ai_coding.tools.builder import build_subagent_runner
from ai_coding.tools.registry import ToolRegistry


def _models() -> dict[str, ModelConfig]:
    return {
        "big": ModelConfig(
            name="big-v1",
            base_url="https://big.test/v1",
            api_key="kb",
            context_window=200_000,
        ),
        "small": ModelConfig(
            name="small-v1",
            base_url="https://small.test/v1",
            api_key="ks",
            context_window=32_000,
        ),
    }


class _Engine:
    """A loop engine that refuses to run a turn (no network here)."""

    def __init__(self, model_config: ModelConfig) -> None:
        self.model_config = model_config
        self.on_retry = None
        self.on_fallback = None

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        raise AssertionError("no model call expected")

    async def aclose(self) -> None:
        return None


def _app_config(models: dict[str, ModelConfig]):
    """The two facts: ``default_model`` is the config's, and it stays ``big``."""
    from types import SimpleNamespace

    return SimpleNamespace(
        models=models,
        default_model="big",
        ai=AIConfig(),
        tools=SimpleNamespace(),
        memory=SimpleNamespace(),
        mcp=SimpleNamespace(),
        agents=SimpleNamespace(),
    )


def _loop(tmp_path, *, models, holder=None, context=None):
    return AgentLoop(
        _Engine(models["big"]),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(),
        models=models,
        default_model="big",
        context=context,
        current_key=holder,
    )


# ---- R-2 子智能体 ------------------------------------------------------------


def test_a_subagent_follows_the_switched_model(tmp_path):
    models = _models()
    holder = ["big"]
    runner = build_subagent_runner(_app_config(models), tmp_path, None, current_key=holder)

    before = runner._engine_factory()
    assert before.model_config.name == "big-v1"

    holder[0] = "small"

    after = runner._engine_factory()
    assert after.model_config.name == "small-v1"
    assert str(after._client.base_url).rstrip("/") == "https://small.test/v1"


def test_switching_the_loop_writes_the_shared_holder(tmp_path):
    """The loop is the only writer; the tool stack reads it (built earlier)."""
    models = _models()
    holder = ["big"]
    loop = _loop(tmp_path, models=models, holder=holder)

    loop.set_model("small")

    assert holder[0] == "small"
    assert loop._current_model_key == "small"


def test_the_config_default_does_not_move_with_the_switch(tmp_path):
    """"What I am using" and "what the config says" are two facts."""
    models = _models()
    holder = ["big"]
    loop = _loop(tmp_path, models=models, holder=holder)

    loop.set_model("small")

    assert loop.default_model == "big"
    assert "small" not in loop.default_model


def test_a_runner_without_a_holder_keeps_the_old_behaviour(tmp_path):
    """The pre-R2 shape still works: no holder, no follow."""
    models = _models()
    runner = build_subagent_runner(_app_config(models), tmp_path, None)

    assert runner._engine_factory().model_config.name == "big-v1"


# ---- R-3 上下文窗口 ----------------------------------------------------------


def _context(tmp_path) -> ContextManager:
    return ContextManager(
        AIConfig(), transcripts_root=tmp_path, summarizer=None, context_window=200_000
    )


def test_the_l3_threshold_is_half_the_window(tmp_path):
    context = _context(tmp_path)

    assert context._max_context_tokens == 200_000
    assert context._max_result_bytes == 100_000


def test_switching_updates_the_context_window(tmp_path):
    models = _models()
    context = _context(tmp_path)
    loop = _loop(tmp_path, models=models, context=context)

    loop.set_model("small")

    assert context._max_context_tokens == 32_000
    # The derived threshold has to move too: a stale one is what let the old
    # model's half keep persisting past the new window.
    assert context._max_result_bytes == 16_000


def test_a_config_override_still_wins_over_the_model_declaration(tmp_path):
    """``ai.max_context_tokens`` is the operator's override, not a default."""
    models = _models()
    context = _context(tmp_path)
    loop = AgentLoop(
        _Engine(models["big"]),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(max_context_tokens=50_000),
        models=models,
        default_model="big",
        context=context,
    )

    loop.set_model("small")

    assert context._max_context_tokens == 50_000
    assert context._max_result_bytes == 25_000


def test_switching_without_a_context_is_fine(tmp_path):
    models = _models()
    loop = _loop(tmp_path, models=models, context=None)

    loop.set_model("small")

    assert loop.engine.model_config.name == "small-v1"
