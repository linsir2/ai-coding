import logging

from typer.testing import CliRunner

from ai_coding.cli import app
from ai_coding.infra.logger_setup import setup_logging

runner = CliRunner()


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
