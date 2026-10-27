"""``model.list`` / ``model.providers`` / ``model.connect`` (H3 红相).

今天没有任何 RPC 能"新增一个模型"，列表也只是 ``initialize`` 那一刻的快照。
这里的断言刻意贴着三条不变量：

* 列表**永不**带出 api_key（它是明文凭据）；
* 连上的模型**当次**就能用（两张目录都更新），且**落盘原文**（``${VAR}``
  不能被解析成明文写进磁盘）；
* 拒绝走 ``{ok: false, reason}``，不抛异常——前端没有 try/except 的语义。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_coding.ai.openai_engine import OpenAIChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.model_config import ModelConfig
from ai_coding.server.handlers import make_handlers
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


class _FakeService:
    """Just enough of ``IpcService`` for the handler table to be built."""

    def __init__(self) -> None:
        self.notified: list[tuple[str, object]] = []

    def notify(self, method: str, params: object = None) -> None:
        self.notified.append((method, params))

    def spawn(self, coro):  # pragma: no cover - no handler under test spawns
        raise AssertionError("unexpected spawn")

    def run(self, coro, timeout=None):  # pragma: no cover
        raise AssertionError("unexpected run")

    async def call(self, method, params=None, timeout=None):  # pragma: no cover
        raise AssertionError("unexpected call")


class _FakeBackend:
    def __init__(self) -> None:
        self.secrets: list[str] = []

    def add_secret_env_names(self, names) -> None:
        for name in names:
            if name not in self.secrets:
                self.secrets.append(name)


def _model(name: str, base_url: str, key: str = "k") -> ModelConfig:
    return ModelConfig(name=name, base_url=base_url, api_key=key)


def _loop(tmp_path) -> AgentLoop:
    models = {
        "alpha": _model("alpha-v1", "https://a.test/v1", "ka"),
        "beta": _model("beta-v1", "https://b.test/v1", "kb"),
    }
    return AgentLoop(
        OpenAIChatEngine(models["alpha"]),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(),
        models=models,
        default_model="alpha",
    )


def _world(tmp_path, *, config_path: str | None = None):
    loop = _loop(tmp_path)
    backend = _FakeBackend()
    world = SimpleNamespace(
        config=SimpleNamespace(
            models=dict(loop.models),
            ai=AIConfig(),
            default_model="alpha",
        ),
        config_path=config_path,
        engine=loop.engine,
        loop=loop,
        workspace=str(tmp_path),
        sandbox_backend=backend,
        notices=None,
        mcp=None,
        approval=SimpleNamespace(set_provider=lambda _p: None),
        user_prompt=SimpleNamespace(set_provider=lambda _p: None),
    )
    return world, backend


def _handlers(world):
    return make_handlers(world, _FakeService())


@pytest.fixture(autouse=True)
def _isolated_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # A missing variable must not leak in from the machine running the tests.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)


def test_model_list_marks_the_active_one(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.list"]({})

    assert result["ok"] is True
    assert result["current"] == "alpha"
    assert result["default"] == "alpha"
    assert [row["key"] for row in result["models"]] == ["alpha", "beta"]


def test_model_list_follows_a_switch_made_outside_the_rpc(tmp_path):
    """``/model x`` typed in the composer goes straight to ``loop.set_model``."""
    world, _backend = _world(tmp_path)

    world.loop.set_model("beta")
    result = _handlers(world)["model.list"]({})

    assert result["current"] == "beta"


def test_model_list_never_sends_the_key(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.list"]({})

    assert "api_key" not in repr(result)
    assert "ka" not in repr(result)
    assert all(row["hasKey"] is True for row in result["models"])


def test_model_providers_publishes_the_table(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.providers"]({})

    ids = [row["id"] for row in result["providers"]]
    assert ids == ["openai", "deepseek", "qwen", "ollama", "generic"]
    openai = result["providers"][0]
    assert openai["envVar"] == "OPENAI_API_KEY"
    assert openai["hasEnv"] is False


def test_connect_persists_and_hot_adds(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "openai", "model": "gpt-5"}
    )

    assert result["ok"] is True, result
    assert result["key"] == "gpt-5"
    assert "gpt-5" in world.config.models
    assert "gpt-5" in world.loop.models
    sidecar = tmp_path / "xdg" / "ai-coding" / "models.local.yaml"
    assert sidecar.is_file()


def test_connect_writes_the_env_reference_when_the_variable_is_set(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    world, _backend = _world(tmp_path)

    _handlers(world)["model.connect"]({"provider": "openai", "model": "gpt-5"})

    body = (tmp_path / "xdg" / "ai-coding" / "models.local.yaml").read_text(
        encoding="utf-8"
    )
    assert "${OPENAI_API_KEY}" in body
    assert "sk-from-env" not in body


def test_a_connected_env_reference_is_stripped_this_session(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    world, backend = _world(tmp_path)

    _handlers(world)["model.connect"]({"provider": "openai", "model": "gpt-5"})

    assert "OPENAI_API_KEY" in backend.secrets


def test_connect_accepts_a_typed_key(tmp_path):
    world, backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "openai", "model": "gpt-5", "apiKey": "sk-typed"}
    )

    assert result["ok"] is True
    assert backend.secrets == [], "a literal key is not an environment variable"


def test_connect_accepts_a_keyless_local_provider(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "ollama", "model": "llama3"}
    )

    assert result["ok"] is True
    assert world.config.models["llama3"].base_url.startswith("http://localhost")


def test_connect_refuses_an_existing_key(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "openai", "model": "alpha", "apiKey": "sk-x"}
    )

    assert result["ok"] is False
    assert "alpha" in result["reason"]
    assert not (tmp_path / "xdg" / "ai-coding" / "models.local.yaml").exists()


def test_connect_refuses_an_unknown_provider(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "deapsek", "model": "x", "apiKey": "sk-x"}
    )

    assert result["ok"] is False
    assert "deapsek" in result["reason"]
    assert "deepseek" in result["reason"]


def test_connect_refuses_a_path_run(tmp_path):
    world, _backend = _world(tmp_path, config_path="/tmp/some-config.yaml")

    result = _handlers(world)["model.connect"](
        {"provider": "openai", "model": "gpt-5", "apiKey": "sk-x"}
    )

    assert result["ok"] is False
    assert "path" in result["reason"].lower()


def test_connect_refuses_custom_without_a_base_url(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "generic", "model": "local-thing", "apiKey": "sk-x"}
    )

    assert result["ok"] is False
    assert "base" in result["reason"].lower()


def test_connect_refuses_a_provider_whose_key_is_missing(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "openai", "model": "gpt-5"}
    )

    assert result["ok"] is False
    assert "OPENAI_API_KEY" in result["reason"]


def test_connect_refuses_an_unset_reference(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.connect"](
        {"provider": "openai", "model": "gpt-5", "apiKey": "${NOPE_NOT_SET}"}
    )

    assert result["ok"] is False
    assert "NOPE_NOT_SET" in result["reason"]


def test_model_set_reports_the_new_endpoint(tmp_path):
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.set"]({"name": "beta"})

    assert result["ok"] is True
    assert result["model"] == "beta"
    assert world.loop.engine.model_config.name == "beta-v1"


def test_the_catalogue_and_the_loop_share_one_table(tmp_path):
    """``world.config.models`` and ``loop.models`` are two dicts; a hot add
    must land in both, or ``/model`` and ``config.summary`` disagree."""
    world, _backend = _world(tmp_path)

    _handlers(world)["model.connect"](
        {"provider": "ollama", "model": "llama3"}
    )

    assert set(world.config.models) == set(world.loop.models)
    assert world.loop.models["llama3"] is world.config.models["llama3"]


def test_model_list_is_synchronous(tmp_path):
    """The TUI calls it from a pool thread; a coroutine would be a bug."""
    world, _backend = _world(tmp_path)

    result = _handlers(world)["model.list"]({})

    assert not asyncio.iscoroutine(result)
