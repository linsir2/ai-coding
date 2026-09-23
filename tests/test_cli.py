import logging

import pytest
from typer.testing import CliRunner

from ai_coding.cli import _build_loop, app
from ai_coding.infra.logger_setup import setup_logging

runner = CliRunner()


def test_build_loop_wires_m3_components(tmp_path):
    """_build_loop assembles context/memory/prompt/skills/project into AgentLoop."""
    from ai_coding.config.models import AppConfig
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "skills").mkdir()
    (ws / "CLAUDE.md").write_text("rules", encoding="utf-8")

    class _DummyEngine:
        pass

    # spec changed in R1a-4: the loop needs the engine + a tool registry, and its
    # settings come from a real AIConfig (it reads stop_policy etc.)
    loop = _build_loop(
        _DummyEngine(),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        AppConfig(),
        str(ws),
        None,
    )
    assert loop.context is not None
    assert loop.memory is not None
    assert loop.prompt is not None
    assert loop.skills is not None
    assert loop.project is not None
    # project file discovered under the workspace
    assert loop.project.load() == "rules"


def test_build_loop_missing_workspace_degrades(tmp_path):
    from ai_coding.config.models import AppConfig
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry

    class _DummyEngine:
        pass

    # workspace does not exist yet — must not raise (silent degradation)
    loop = _build_loop(
        _DummyEngine(),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        AppConfig(),
        str(tmp_path / "nonexistent"),
        None,
    )
    assert loop.context is not None
    assert loop.project.load() == ""
    assert loop.skills.is_empty is True


@pytest.mark.asyncio
async def test_build_loop_runs_a_turn_through_a_real_tool(tmp_path):
    """End-to-end through the CLI wiring: loop -> dispatcher -> real read tool."""
    from ai_coding.ai.engine import ChatEngine
    from ai_coding.config.models import AppConfig
    from ai_coding.domain.loop import StepResult
    from ai_coding.domain.message import ToolCallRef
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.builder import build_tool_registry

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sample.txt").write_text("hello world\n", encoding="utf-8")

    class _Engine(ChatEngine):
        def __init__(self) -> None:
            self.requests: list = []

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self.requests.append(messages)
            if len(self.requests) == 1:
                return StepResult(
                    tool_calls=[
                        ToolCallRef(
                            id="c1", name="read", arguments='{"path":"sample.txt"}'
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return StepResult(text="read it", finish_reason="stop")

    engine = _Engine()
    app_cfg = AppConfig()
    registry = build_tool_registry(app_cfg, str(ws), None, include_subagent=False)
    sessions = SessionService(tmp_path / "sessions")
    loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None)

    session = sessions.create()
    text = await loop.process_input(session, "read sample.txt")
    await loop.aclose()

    assert text == "read it"
    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "hello world" in (tool_messages[0].content or "")


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
