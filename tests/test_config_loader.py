import textwrap

import pytest
from pydantic import ValidationError

from ai_coding.config.loader import ConfigLoader, ConfigManager
from ai_coding.config.models import AIConfig, AppConfig, MemoryConfig
from ai_coding.domain.model_config import ModelConfig


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    """Isolate the "nothing found" case from this repo's own config.local.yaml.

    R10 made ``load_app(None)`` discover a config by walking up from the cwd, so
    a test that wants bare defaults has to stand somewhere with no config above
    it — otherwise the repo's real settings leak into the assertion.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    for parent in (empty, *empty.parents):
        if (parent / "config.local.yaml").is_file():
            pytest.skip("a parent directory holds a config; cannot isolate defaults")
    monkeypatch.chdir(empty)
    return empty


def test_load_returns_defaults_on_none(no_config):
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


def test_mcp_defaults(no_config):
    mcp = ConfigLoader.load_mcp(None)
    assert mcp.enabled is False
    assert mcp.auto_discover is True
    # R7d: 30s left ~3s of headroom against a measured 27s npx cold start, so the
    # default is now 60 (docs/r7-analysis.md 3.4, r7-backlog.md 3.3).
    assert mcp.connection_timeout == 60
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


# ---- R4a: tracing and pricing configuration --------------------------------


def test_tracing_block_has_working_defaults_and_can_be_configured(tmp_path):
    default = ConfigLoader.load_app(None)
    assert default.tracing.enabled is True
    assert default.tracing.capture == "previews"
    assert default.tracing.max_turn_bytes == 2_000_000
    assert default.tracing.retention_days == 14
    assert default.tracing.redact is True
    assert default.tracing.dir == ""

    cfg = tmp_path / "t.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            tracing:
              enabled: false
              capture: full
              maxTurnBytes: 1000
              retentionDays: 3
            """
        ),
        encoding="utf-8",
    )
    loaded = ConfigLoader.load_app(cfg)
    assert loaded.tracing.enabled is False
    assert loaded.tracing.capture == "full"
    assert loaded.tracing.max_turn_bytes == 1000
    assert loaded.tracing.retention_days == 3


def test_model_prices_are_optional_and_default_to_zero(tmp_path, no_config):
    default = ConfigLoader.load_app(None)
    assert default.models == {}

    cfg = tmp_path / "p.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            default_model: qwen
            models:
              qwen:
                name: qwen3.8-max
                base_url: https://example.invalid/v1
                api_key: k
                price_input_per_1k: 0.002
                price_output_per_1k: 0.006
              plain:
                name: plain
                base_url: https://example.invalid/v1
                api_key: k
            """
        ),
        encoding="utf-8",
    )
    loaded = ConfigLoader.load_app(cfg)
    assert loaded.models["qwen"].price_input_per_1k == 0.002
    assert loaded.models["qwen"].price_output_per_1k == 0.006
    assert loaded.models["plain"].price_input_per_1k == 0.0
    assert loaded.models["plain"].price_output_per_1k == 0.0


def test_a_config_without_the_new_blocks_still_loads(tmp_path):
    """Every R4 block is optional, so existing configs keep working."""
    cfg = tmp_path / "old.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            default_model: m
            models:
              m:
                name: m
                base_url: https://example.invalid/v1
                api_key: k
            ai:
              max_tool_iterations: 3
            """
        ),
        encoding="utf-8",
    )
    loaded = ConfigLoader.load_app(cfg)
    assert loaded.ai.max_tool_iterations == 3
    assert loaded.tracing.enabled is True
    assert loaded.guard.duplicate_threshold == 2
    assert loaded.hooks.enabled is False


# ---- R7e: the switches, and the fact that every default is today's behaviour ----


def test_r7e_defaults_keep_every_behaviour_unchanged():
    ai = AIConfig()
    assert ai.subagent_context_mode == "full"  # Q8: 今天的行为
    assert ai.tools_chars_warn == 12_000  # Q7
    assert ai.steering_max_messages == 5  # Q6
    assert ai.steering_max_chars == 2_000  # Q6
    assert AppConfig().ui.steering is True  # Q6


def test_an_unknown_subagent_context_mode_is_rejected_at_load():
    with pytest.raises(ValidationError):
        AIConfig(subagent_context_mode="clean")


def test_the_ui_block_is_independent_of_the_rest():
    cfg = AppConfig.model_validate({"ui": {"steering": False}})
    assert cfg.ui.steering is False
    assert cfg.ai.subagent_context_mode == "full"


def test_r11b_memory_recall_timeout_default_and_camel(tmp_path):
    """R11b: the recall budget defaults to 2.0s and loads from camelCase YAML."""
    assert MemoryConfig().recall_timeout_seconds == 2.0
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            default_model: x
            models:
              x: {name: x, base_url: http://a, api_key: k}
            memory:
              recallTimeoutSeconds: 0
            """
        )
    )
    assert ConfigLoader.load_app(str(cfg)).memory.recall_timeout_seconds == 0


def test_r11d_fusion_defaults_to_blend_and_rejects_unknown_values():
    """R11d: RRF ships available and off. A bad value fails at load, not mid-recall."""
    assert MemoryConfig().recall_fusion == "blend"
    assert MemoryConfig().rrf_k == 60
    assert MemoryConfig(recall_fusion="rrf").recall_fusion == "rrf"
    with pytest.raises(ValidationError):
        MemoryConfig(recall_fusion="rrf-ish")


def test_r11d_fusion_loads_from_camel_case_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            default_model: x
            models:
              x: {name: x, base_url: http://a, api_key: k}
            memory:
              recallFusion: rrf
              rrfK: 30
            """
        )
    )
    memory = ConfigLoader.load_app(str(cfg)).memory
    assert memory.recall_fusion == "rrf"
    assert memory.rrf_k == 30
