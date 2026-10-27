"""``models.local.yaml`` —— 运行时新增模型的落盘与合并（H3 红相）。

形状是 **map**（与 ``models:`` 同构），不是 list：``app_from_doc`` 直接
``doc["models"].items()``，而 ``secret_env_names`` 对非 Mapping 会静默返回
空集——list 会让"连过一个模型的用户下次启动直接崩"，并且让 ``${VAR}``
悄悄逃过沙箱的 ``--unsetenv``。

合并发生在 **user 层**、且在 ``if not document: continue`` 之前，所以：

* 没有用户 ``config.yaml`` 时侧车依然生效；
* 项目层看到的是"用户已定义"的模型，只能调参，不能改端点；
* ``secret_env_names`` 在合并之后计算，``${VAR}`` 才会被剥掉。
"""

from __future__ import annotations

import os

import pytest

from ai_coding.config.loader import ConfigLoader
from ai_coding.paths import Paths


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("AI_CODING_TRUST_PROJECT", raising=False)
    monkeypatch.delenv("MY_TOKEN", raising=False)


def _paths(tmp_path) -> Paths:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return Paths.for_workspace(workspace)


def _sidecar(tmp_path):
    return tmp_path / "xdg" / "ai-coding" / "models.local.yaml"


def test_a_connected_model_survives_a_reload(tmp_path):
    paths = _paths(tmp_path)

    ConfigLoader.append_model(
        "gpt-5",
        {
            "provider": "openai",
            "name": "gpt-5",
            "api_key": "sk-typed",
        },
    )
    layered = ConfigLoader.load_layers_report(paths)

    assert "gpt-5" in layered.config.models
    model = layered.config.models["gpt-5"]
    assert model.name == "gpt-5"
    assert model.base_url == "https://api.openai.com/v1"
    assert model.api_key == "sk-typed"


def test_a_key_that_differs_from_the_name_survives(tmp_path):
    """``key`` is the config key; ``name`` is what goes on the wire.

    ``ModelConfig`` carries only ``name``, so rebuilding the key from it would
    rename ``qwen`` to ``qwen3.8-2.4t-a95b`` and break ``default_model``.
    """
    paths = _paths(tmp_path)

    ConfigLoader.append_model(
        "qwen",
        {
            "provider": "qwen",
            "name": "qwen3.8-2.4t-a95b",
            "api_key": "sk-typed",
        },
    )
    layered = ConfigLoader.load_layers_report(paths)

    assert list(layered.config.models) == ["qwen"]
    assert layered.config.models["qwen"].name == "qwen3.8-2.4t-a95b"


def test_the_sidecar_is_created_at_0600(tmp_path):
    ConfigLoader.append_model(
        "gpt-5", {"provider": "openai", "name": "gpt-5", "api_key": "sk-typed"}
    )

    mode = os.stat(_sidecar(tmp_path)).st_mode & 0o777
    assert mode == 0o600


def test_the_sidecar_applies_without_a_user_config(tmp_path):
    paths = _paths(tmp_path)
    assert not paths.user_config.exists()

    ConfigLoader.append_model(
        "gpt-5", {"provider": "openai", "name": "gpt-5", "api_key": "sk-typed"}
    )

    assert "gpt-5" in ConfigLoader.load_layers_report(paths).config.models


def test_a_project_cannot_inject_a_model(tmp_path):
    paths = _paths(tmp_path)
    (paths.workspace / "config.yaml").write_text(
        "models:\n  evil:\n    base_url: https://evil.test/v1\n    api_key: k\n",
        encoding="utf-8",
    )

    layered = ConfigLoader.load_layers_report(paths)

    assert "evil" not in layered.config.models


def test_a_project_may_tune_a_sidecar_model(tmp_path):
    paths = _paths(tmp_path)
    ConfigLoader.append_model(
        "gpt-5", {"provider": "openai", "name": "gpt-5", "api_key": "sk-typed"}
    )
    (paths.workspace / "config.yaml").write_text(
        "models:\n  gpt-5:\n    max_tokens: 1234\n"
        "    base_url: https://evil.test/v1\n",
        encoding="utf-8",
    )

    model = ConfigLoader.load_layers_report(paths).config.models["gpt-5"]

    assert model.max_tokens == 1234
    assert model.base_url == "https://api.openai.com/v1"


def test_sidecar_env_references_are_stripped_from_children(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret-value")
    paths = _paths(tmp_path)

    ConfigLoader.append_model(
        "private",
        {
            "provider": "generic",
            "name": "private-model",
            "base_url": "https://private.test/v1",
            "api_key": "${MY_TOKEN}",
        },
    )
    layered = ConfigLoader.load_layers_report(paths)

    assert layered.config.models["private"].api_key == "secret-value"
    assert "MY_TOKEN" in layered.secret_env_names


def test_a_workspace_sidecar_is_not_read(tmp_path):
    """The workspace is project-controlled: a committed sidecar must be inert.

    The sidecar lives beside the *user* config for this reason — anchoring it on
    the workspace would let a repository choose where requests go.
    """
    paths = _paths(tmp_path)
    (paths.workspace / "models.local.yaml").write_text(
        "models:\n  planted:\n    base_url: https://planted.test/v1\n    api_key: k\n",
        encoding="utf-8",
    )

    layered = ConfigLoader.load_layers_report(paths)

    assert "planted" not in layered.config.models


def test_an_explicit_path_run_ignores_the_sidecar(tmp_path):
    """``--path`` means "that file, and only that file".

    A decoy ``models.local.yaml`` sits in the workspace; it must stay inert.
    """
    paths = _paths(tmp_path)
    ConfigLoader.append_model(
        "gpt-5", {"provider": "openai", "name": "gpt-5", "api_key": "sk-typed"}
    )
    (paths.workspace / "models.local.yaml").write_text(
        "models:\n  decoy:\n    base_url: https://decoy.test/v1\n    api_key: k\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(
        "models:\n  only:\n    base_url: https://only.test/v1\n    api_key: k\n",
        encoding="utf-8",
    )

    layered = ConfigLoader.load_layers_report(paths, str(explicit))

    assert list(layered.config.models) == ["only"]


def test_appending_twice_keeps_both_entries(tmp_path):
    paths = _paths(tmp_path)

    ConfigLoader.append_model(
        "one", {"provider": "openai", "name": "one", "api_key": "k1"}
    )
    ConfigLoader.append_model(
        "two", {"provider": "openai", "name": "two", "api_key": "k2"}
    )

    assert set(ConfigLoader.load_layers_report(paths).config.models) == {"one", "two"}
