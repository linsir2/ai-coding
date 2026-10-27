"""M-H2: `provider` in a model entry, and `ai-coding config init` / `config path`.

The onboarding defect this closes, measured: a workspace whose only config is
`config.local.yaml` (what `config.example.yaml` used to tell you to create) is
read as the **project** layer, whose `models` entries are dropped by the trust
filter — so `models` ends up empty and the CLI says "no default model
configured".  The fix is not to weaken that filter (a repository must not choose
your endpoint or your credential); it is to make the *user* layer writable.
"""

from __future__ import annotations

import stat

import pytest
from typer.testing import CliRunner

from ai_coding.cli import app
from ai_coding.config.loader import ConfigLoader
from ai_coding.config.models import _coerce_model
from ai_coding.paths import Paths, config_home

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- `provider` in a model entry -------------------------------------------


def test_a_provider_supplies_the_default_base_url():
    cfg = _coerce_model({"provider": "deepseek", "name": "x"}, env={})

    assert cfg.base_url == "https://api.deepseek.com/v1"


def test_an_explicit_base_url_beats_the_provider_default():
    cfg = _coerce_model(
        {"provider": "deepseek", "base_url": "https://proxy.internal/v1", "name": "x"},
        env={},
    )

    assert cfg.base_url == "https://proxy.internal/v1"


def test_an_empty_yaml_value_is_not_the_string_none():
    """`api_key:` with no value parses as None; `str(None)` would be a key."""
    cfg = _coerce_model({"name": "x", "base_url": None, "api_key": None}, env={})

    assert cfg.base_url == ""
    assert cfg.api_key == ""


def test_a_project_cannot_choose_the_provider(home):
    """`provider` picks the endpoint *and* the env var, so it is a credential selector.

    The user entry has no `base_url`, so the provider alone decides it: if the
    project's `provider: openai` survived the filter, the endpoint would move to
    OpenAI's and the key would be looked up in `OPENAI_API_KEY`.
    """
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "models:\n  m:\n    name: m\n    provider: deepseek\n    api_key: sk-user\n",
    )
    _write(
        home / "project" / "config.local.yaml",
        "models:\n  m:\n    provider: openai\n",
    )

    app_cfg = ConfigLoader.load_layers(paths, env={})

    assert app_cfg.models["m"].base_url == "https://api.deepseek.com/v1"


# ---- config init / config path ---------------------------------------------


def test_config_path_prints_the_user_layer(home):
    result = runner.invoke(app, ["config", "path"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(config_home() / "config.yaml")


def test_config_init_writes_the_user_layer_at_0600(home):
    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0, result.output
    path = config_home() / "config.yaml"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_home().stat().st_mode) == 0o700

    # The file it writes must actually load — with the variable supplied.
    app_cfg = ConfigLoader.load_app(path, env={"DEEPSEEK_API_KEY": "x"})
    assert list(app_cfg.models) == ["deepseek-chat"]
    assert app_cfg.default_model == "deepseek-chat"


def test_config_init_refuses_to_overwrite_without_force(home):
    path = config_home() / "config.yaml"
    _write(path, "default_model: keep-me\n")

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code != 0
    assert path.read_text(encoding="utf-8") == "default_model: keep-me\n"


def test_config_init_overwrites_with_force(home):
    path = config_home() / "config.yaml"
    _write(path, "default_model: old\n")

    result = runner.invoke(app, ["config", "init", "--force"])

    assert result.exit_code == 0, result.output
    body = path.read_text(encoding="utf-8")
    assert "default_model: old" not in body
    assert "provider: deepseek" in body
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---- the end-to-end path the docs now describe ------------------------------


def test_the_documented_onboarding_path_works(home, monkeypatch):
    """`config init` → export the key → start, with no `--path` and no project file.

    This is the exact sequence the docs teach, and it used to end at
    `models: []` / `default_model: ''`.
    """
    assert runner.invoke(app, ["config", "init"]).exit_code == 0
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")

    layered = ConfigLoader.load_layers_report(Paths.for_workspace(home / "project"))

    assert list(layered.config.models) == ["deepseek-chat"]
    assert layered.config.default_model == "deepseek-chat"
    assert layered.config.models["deepseek-chat"].api_key == "sk-from-env"
    # ... and the sandbox knows which variable it must strip from children.
    assert "DEEPSEEK_API_KEY" in layered.secret_env_names


# ---- review fixes: the error must say *which* model ------------------------


def test_a_missing_secret_names_the_model(tmp_path):
    """A multi-model config with one bad entry must not answer with a bare variable."""
    path = _write(
        tmp_path / "c.yaml",
        "models:\n  backup:\n    name: backup\n    provider: deepseek\n"
        "    api_key: ${NOT_SET_ANYWHERE}\n",
    )

    with pytest.raises(Exception) as caught:
        ConfigLoader.load_app(path, env={})

    message = str(caught.value)
    assert "backup" in message, message
    assert "NOT_SET_ANYWHERE" in message, message


def test_an_unknown_provider_names_the_model(tmp_path):
    path = _write(
        tmp_path / "c.yaml",
        "models:\n  main:\n    name: main\n    provider: deapsek\n",
    )

    with pytest.raises(Exception) as caught:
        ConfigLoader.load_app(path, env={})

    message = str(caught.value)
    assert "main" in message, message
    assert "deapsek" in message, message


def test_a_mismatched_provider_and_base_url_warns(caplog):
    """`provider` wins, so A's key would go to B's endpoint with no word said."""
    import logging

    with caplog.at_level(logging.WARNING, logger="ai_coding.config.models"):
        cfg = _coerce_model(
            {
                "name": "x",
                "provider": "deepseek",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-deepseek",
            },
            env={},
        )

    assert cfg.base_url == "https://api.openai.com/v1"  # behaviour unchanged
    assert any(
        "openai" in record.message and "deepseek" in record.message
        for record in caplog.records
    ), [record.message for record in caplog.records]
