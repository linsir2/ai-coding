import textwrap

from ai_coding.config.loader import ConfigLoader, ConfigManager
from ai_coding.domain.model_config import ModelConfig


def test_load_returns_defaults_on_none():
    app = ConfigLoader.load_app(None)
    assert app.default_model == ""
    assert app.models == {}
    assert app.ai.max_context_tokens == 48000
    assert app.ai.max_tool_iterations == 10


def test_load_from_yaml_file_overrides(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            default_model: my-model
            ai:
              max_context_tokens: 12345
              max_messages: 99
            """
        ),
        encoding="utf-8",
    )
    app = ConfigLoader.load_app(cfg)
    assert app.default_model == "my-model"
    assert app.ai.max_context_tokens == 12345
    assert app.ai.max_messages == 99
    # non-overridden defaults remain
    assert app.ai.snip_keep_tail == 20


def test_load_models_camelcase_compat(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            models:
              deepseek-chat:
                name: deepseek-chat
                baseURL: https://api.deepseek.com/v1
                apiKey: sk-test
                maxTokens: 2048
            default_model: deepseek-chat
            """
        ),
        encoding="utf-8",
    )
    app = ConfigLoader.load_app(cfg)
    m = app.models["deepseek-chat"]
    assert m.base_url == "https://api.deepseek.com/v1"
    assert m.api_key == "sk-test"
    assert m.max_tokens == 2048
    assert app.default_model == "deepseek-chat"


def test_load_unknown_keys_ignored():
    from ai_coding.config.models import AppConfig

    app = AppConfig.model_validate({"unknown_root": 1, "ai": {"not_a_key": True}})
    assert app.ai.max_context_tokens == 48000


def test_tools_default_enabled():
    app = ConfigLoader.load_app(None)
    assert app.tools.bash.enabled is True
    assert app.tools.read.enabled is True
    assert app.tools.read.max_file_size == 10485760


def test_mcp_defaults():
    mcp = ConfigLoader.load_mcp(None)
    assert mcp.enabled is False
    assert mcp.auto_discover is True
    assert mcp.connection_timeout == 30
    assert mcp.servers == []


def test_model_config_defaults():
    m = ModelConfig(name="n", base_url="b", api_key="k")
    assert m.streaming is True
    assert m.max_tokens == 4096
    assert m.temperature == 0.7
    assert m.top_p == 0.9
    assert m.timeout == 60


def test_missing_file_falls_back_defaults_for_manager_init(tmp_path):
    mgr = ConfigManager()
    mgr.initialize(None)
    assert mgr.mcp.enabled is False
    assert mgr.app.ai.max_context_tokens == 48000


def test_missing_explicit_path_raises(tmp_path):
    import pytest

    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError):
        ConfigLoader.load_app(missing)


def test_mcp_servers_from_yaml(tmp_path):
    cfg = tmp_path / "m.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            mcp:
              enabled: true
              servers:
                - name: github
                  command: npx
                  args: ["-y", "@modelcontextprotocol/server-github"]
            """
        ),
        encoding="utf-8",
    )
    mcp = ConfigLoader.load_mcp(cfg)
    assert mcp.enabled is True
    assert mcp.servers[0].name == "github"
    assert mcp.servers[0].command == "npx"
    assert mcp.servers[0].enabled is False
