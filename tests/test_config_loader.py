import textwrap

import pytest
from pydantic import ValidationError

from ai_coding.config.loader import ConfigLoader, ConfigManager
from ai_coding.config.models import AIConfig, AppConfig, MemoryConfig, resolve_context_window
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
    # The raw field is 0 = "ask the model"; what a caller actually needs is the
    # window the policy and the meter will use, and an undeclared model must
    # still resolve to the conservative default rather than an invented large one.
    assert resolve_context_window(app) == 48_000
    assert app.ai.max_tool_iterations == 100
    # Stage 2: Plan-as-Goal (main-session rounds) is the default executor;
    # wave and legacy stay one setting away.
    assert app.ai.plan_orchestrator == "session"
    assert app.ai.plan_wave_size == 4
    # A+C (measured): one real item spent 269k tokens, so a per-wave spend cap
    # ended a real run after one wave.  Both segment caps are off by default;
    # the episode ceiling and the no-progress brake are the operator's levers.
    assert app.ai.plan_wave_token_budget == 0
    assert app.ai.plan_wave_wall_seconds == 0.0
    assert app.ai.plan_no_progress_waves == 3
    assert app.ai.plan_token_budget == 0
    assert app.ai.plan_max_item_attempts == 2


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
    assert app.ai.snip_keep_tail == 30


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

    app = AppConfig.model_validate({"unknown_root": 1, "ai": {"not_a_key": True}})
    assert resolve_context_window(app) == 48_000


def test_tools_default_enabled():
    app = ConfigLoader.load_app(None)
    assert app.tools.bash.enabled is True
    assert app.tools.read.enabled is True
    assert app.tools.read.max_file_size == 10485760


def test_mcp_defaults(no_config):
    mcp = ConfigLoader.load_mcp(None)
    assert mcp.enabled is False
    # R7d: 30s left ~3s of headroom against a measured 27s npx cold start, so the
    # default is now 60s (docs/r7-analysis.md 3.4, r7-backlog.md 3.3).  The
    # timeout defaults themselves live on MCPServerConfig (test_mcp.py).
    assert mcp.servers == []


def test_model_config_defaults():
    m = ModelConfig(name="n", base_url="b", api_key="k")
    assert m.streaming is True
    assert m.max_tokens == 4096
    assert m.temperature == 0.7
    assert m.top_p == 0.9
    assert m.timeout == 60


def test_missing_file_falls_back_defaults_for_manager_init(tmp_path, monkeypatch):
    """Runs from a directory with no config above it, because ``initialize(None)``
    discovers one by walking up from the cwd.

    It used to run from the repo, where ``config.local.yaml`` happened to say
    ``mcp.enabled: false`` — so the assertion was really testing the developer's
    own file, and went red the moment anyone switched MCP on.  Fixed here by
    isolating the lookup, which is what the test's name has always claimed.
    """
    monkeypatch.chdir(tmp_path)

    mgr = ConfigManager()
    mgr.initialize(None)
    assert mgr.mcp.enabled is False
    assert resolve_context_window(mgr.app) == 48_000


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
                  command: ["npx", "-y", "@modelcontextprotocol/server-github"]
                  environment: {TOKEN: "ghp_x"}
                  timeout:
                    startup: 45000
                    catalog: 15000
            """
        ),
        encoding="utf-8",
    )
    mcp = ConfigLoader.load_mcp(cfg)
    assert mcp.enabled is True
    assert mcp.servers[0].name == "github"
    # argv is one list, as in OpenCode v2, so pasting an OpenCode config works.
    assert mcp.servers[0].command == ["npx", "-y", "@modelcontextprotocol/server-github"]
    assert mcp.servers[0].environment == {"TOKEN": "ghp_x"}
    # The v2 polarity: a server is on unless it says otherwise.
    assert mcp.servers[0].disabled is False
    assert mcp.servers[0].timeout.startup == 45_000
    assert mcp.servers[0].timeout.catalog == 15_000


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
    assert ai.mcp_chars_warn == 2_000  # r22: 只对 MCP 计费，本地工具用户改不了
    assert ai.steering_max_messages == 5  # Q6
    assert ai.steering_max_chars == 2_000  # Q6


def test_an_unknown_subagent_context_mode_is_rejected_at_load():
    with pytest.raises(ValidationError):
        AIConfig(subagent_context_mode="clean")


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


def test_r_p0_1_model_retry_budget_loads_and_clamps(tmp_path):
    """The retry budget is per-model transport config; 0 must survive as "off"
    and a negative value must not become extra attempts."""
    from ai_coding.config.loader import ConfigLoader

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
        default_model: a
        models:
          a:
            name: m
            base_url: "https://example.invalid/v1"
            api_key: k
            max_retries: 5
            retry_base_seconds: 1.5
          b: {name: m, base_url: "https://example.invalid/v1", api_key: k, max_retries: 0}
          c: {name: m, base_url: "https://example.invalid/v1", api_key: k, max_retries: -4}
        """,
        encoding="utf-8",
    )
    loaded = ConfigLoader.load_app(cfg)

    assert loaded.models["a"].max_retries == 5
    assert loaded.models["a"].retry_base_seconds == 1.5
    assert loaded.models["b"].max_retries == 0
    assert loaded.models["c"].max_retries == 0


def test_r_p0_1_model_retry_budget_defaults_to_three(tmp_path):
    from ai_coding.config.loader import ConfigLoader

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
        default_model: a
        models:
          a: {name: m, base_url: "https://example.invalid/v1", api_key: k}
        """,
        encoding="utf-8",
    )
    loaded = ConfigLoader.load_app(cfg)

    assert loaded.models["a"].max_retries == 3
    assert loaded.models["a"].retry_base_seconds == 2.0


# ---- M2: the fallback chain lives on the model it belongs to ---------------


def _write_models(tmp_path, body: str):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(textwrap.dedent(body), encoding="utf-8")
    return ConfigLoader.load_app(cfg)


def test_a_model_without_fallbacks_declares_none(tmp_path):
    """The default has to be "no chain": the factory keys its short-circuit on
    it, and every config written before M2 takes this path."""
    app = _write_models(
        tmp_path,
        """
        models:
          m:
            name: m
            base_url: https://a.test/v1
            api_key: k
        default_model: m
        """,
    )

    assert app.models["m"].fallback_models == ()


def test_fallback_models_parse_as_a_tuple_of_names(tmp_path):
    app = _write_models(
        tmp_path,
        """
        models:
          primary:
            name: primary
            base_url: https://a.test/v1
            api_key: k
            fallback_models: [backup]
          backup:
            name: backup
            base_url: https://b.test/v1
            api_key: k
        default_model: primary
        """,
    )

    assert app.models["primary"].fallback_models == ("backup",)


def test_a_single_fallback_may_be_written_without_brackets(tmp_path):
    """YAML reads ``fallback_models: backup`` as a plain string, and iterating
    that yields one item per *character*.  A chain would then be built from the
    letters b-a-c-k-u-p, silently, and only when the first model fails."""
    app = _write_models(
        tmp_path,
        """
        models:
          primary:
            name: primary
            base_url: https://a.test/v1
            api_key: k
            fallback_models: backup
          backup:
            name: backup
            base_url: https://b.test/v1
            api_key: k
        default_model: primary
        """,
    )

    assert app.models["primary"].fallback_models == ("backup",)


# ---- M5: the mcp.local.yaml sidecar ----------------------------------------


def test_the_sidecar_merges_its_servers_in(tmp_path):
    from ai_coding.config.loader import ConfigLoader
    from ai_coding.config.models import MCPServerConfig

    cfg = tmp_path / "config.local.yaml"
    cfg.write_text(
        "models:\n  m:\n    name: m\nmcp:\n  enabled: true\n  servers:\n"
        "    - name: from-config\n      command: [a]\n",
        encoding="utf-8",
    )
    ConfigLoader.append_mcp_server(cfg, MCPServerConfig(name="added", command=["b"]))

    mcp = ConfigLoader.load_mcp(cfg)

    # The main file's server first, then the wizard's — in that order.
    assert [s.name for s in mcp.servers] == ["from-config", "added"]
    assert mcp.enabled is True


def test_the_sidecar_merges_into_the_app_config_too(tmp_path):
    """The app's own config carries the MCP root the world builds from.

    ``load_mcp`` was not the only way in: ``AppConfig.mcp`` is what
    ``build_world`` hands the manager, so a sidecar server the wizard added has
    to be in that one too or it is invisible until the next ``mcp.add``.
    """
    from ai_coding.config.loader import ConfigLoader
    from ai_coding.config.models import MCPServerConfig

    cfg = tmp_path / "config.local.yaml"
    cfg.write_text("mcp:\n  enabled: true\n", encoding="utf-8")
    ConfigLoader.append_mcp_server(cfg, MCPServerConfig(name="added", command=["b"]))

    app = ConfigLoader.load_app(cfg)

    assert [s.name for s in app.mcp.servers] == ["added"]
    assert app.mcp.enabled is True


def test_the_sidecar_is_optional_and_secret(tmp_path):
    from ai_coding.config.loader import ConfigLoader
    from ai_coding.config.models import MCPServerConfig

    cfg = tmp_path / "config.local.yaml"
    cfg.write_text("mcp:\n  enabled: true\n", encoding="utf-8")
    assert ConfigLoader.mcp_sidecar(cfg) == tmp_path / "mcp.local.yaml"
    mcp = ConfigLoader.load_mcp(cfg)
    assert mcp.servers == []

    ConfigLoader.append_mcp_server(cfg, MCPServerConfig(name="s", command=["x"]))
    mode = oct((tmp_path / "mcp.local.yaml").stat().st_mode & 0o777)
    assert mode == "0o600"
    # The operator's own file is untouched by the append.
    assert "s" not in cfg.read_text(encoding="utf-8")


def test_a_broken_sidecar_is_ignored_not_fatal(tmp_path):
    cfg = tmp_path / "config.local.yaml"
    cfg.write_text("mcp:\n  enabled: true\n", encoding="utf-8")
    (tmp_path / "mcp.local.yaml").write_text("- : : not yaml [\n", encoding="utf-8")

    mcp = ConfigLoader.load_mcp(cfg)

    assert mcp.servers == []


# ---- P3-6: an empty read denylist must not be silent -----------------------


def test_an_empty_read_deny_list_warns_at_load_time(caplog):
    """P3-6: ``read_deny_paths: []`` closes the read boundary; say so at load."""
    import logging

    from ai_coding.config.models import ProcessSandboxConfig

    with caplog.at_level(logging.WARNING):
        ProcessSandboxConfig(read_deny_paths=[])
    assert any("read_deny_paths is empty" in record.message for record in caplog.records)


def test_the_default_read_deny_list_does_not_warn(caplog):
    """P3-6: the warning is for an explicit empty list, not the default."""
    import logging

    from ai_coding.config.models import ProcessSandboxConfig

    with caplog.at_level(logging.WARNING):
        ProcessSandboxConfig()
    assert not [
        record for record in caplog.records if "read_deny_paths is empty" in record.message
    ]
