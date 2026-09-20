import logging

from typer.testing import CliRunner

from ai_coding.cli import _build_loop, app
from ai_coding.infra.logger_setup import setup_logging

runner = CliRunner()


def test_build_loop_wires_m3_components(tmp_path):
    """_build_loop assembles context/memory/prompt/skills/project into AgentLoop."""
    from types import SimpleNamespace

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "skills").mkdir()
    (ws / "CLAUDE.md").write_text("rules", encoding="utf-8")

    app_cfg = SimpleNamespace(
        memory=SimpleNamespace(enabled=True, consolidate_threshold=100,
                               max_index_entries=100, max_per_turn_injections=3),
        ai=SimpleNamespace(max_context_tokens=48000, max_messages=50, snip_keep_head=3,
                           snip_keep_tail=20, keep_recent_tool_results=3,
                           per_result_persist_bytes=30000, l4_keep_tail=6),
    )

    class _Dummy:
        pass

    loop = _build_loop(_Dummy(), _Dummy(), app_cfg, str(ws), None)
    assert loop.context is not None
    assert loop.memory is not None
    assert loop.prompt is not None
    assert loop.skills is not None
    assert loop.project is not None
    # project file discovered under the workspace
    assert loop.project.load() == "rules"


def test_build_loop_missing_workspace_degrades(tmp_path):
    from types import SimpleNamespace

    app_cfg = SimpleNamespace(
        memory=SimpleNamespace(enabled=True, consolidate_threshold=100,
                               max_index_entries=100, max_per_turn_injections=3),
        ai=SimpleNamespace(max_context_tokens=48000, max_messages=50, snip_keep_head=3,
                           snip_keep_tail=20, keep_recent_tool_results=3,
                           per_result_persist_bytes=30000, l4_keep_tail=6),
    )

    class _Dummy:
        pass

    # workspace does not exist yet — must not raise (silent degradation)
    loop = _build_loop(_Dummy(), _Dummy(), app_cfg, str(tmp_path / "nonexistent"), None)
    assert loop.context is not None
    assert loop.project.load() == ""
    assert loop.skills.is_empty is True


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_config_check_valid(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "default_model: deepseek-chat\nmodels:\n  deepseek-chat:\n"
        "    name: deepseek-chat\n    base_url: https://api.deepseek.com/v1\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config-check", "--path", str(cfg)])
    assert result.exit_code == 0
    assert "deepseek-chat" in result.stdout


def test_config_check_missing_file():
    result = runner.invoke(app, ["config-check", "--path", "/tmp/definitely-absent.yaml"])
    assert result.exit_code != 0
    assert "config error" in (result.stderr or result.stdout)


def test_setup_logging_idempotent():
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        assert setup_logging() is True
        assert setup_logging() is False
    finally:
        root.handlers = saved


def test_ask_no_default_model_errors_nonzero(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("models:\n  m:\n    name: m\n    base_url: https://x\n    api_key: k\n",
                   encoding="utf-8")
    # no default_model -> factory raises -> CLI must exit non-zero with a message
    result = runner.invoke(app, ["ask", "--prompt", "hi", "--path", str(cfg)])
    assert result.exit_code == 1
    assert "no default model" in (result.stderr or result.stdout)


def test_ask_workspace_flag_accepted(tmp_path):
    """The --workspace flag should be accepted (offline path still fails on no model)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("models:\n  m:\n    name: m\n    base_url: https://x\n    api_key: k\n",
                   encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    result = runner.invoke(app, [
        "ask", "--prompt", "hi", "--path", str(cfg), "--workspace", str(ws),
    ])
    # Still fails on no default model, but the flag is parsed OK (not exit 2)
    assert result.exit_code == 1
    assert "no default model" in (result.stderr or result.stdout)


def test_repl_no_default_model_errors_nonzero(tmp_path):
    """`repl` registers as a command and degrades cleanly without a default model."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("models:\n  m:\n    name: m\n    base_url: https://x\n    api_key: k\n",
                   encoding="utf-8")
    result = runner.invoke(app, ["repl", "--path", str(cfg), "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "no default model" in (result.stderr or result.stdout)
