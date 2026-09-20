"""ai-coding entry point (Typer). M0 exposes a minimal but functional shell."""

from __future__ import annotations

import asyncio

import typer

from ai_coding.ai.factory import build_service_from_config
from ai_coding.config.loader import ConfigLoader, ConfigManager
from ai_coding.infra.logger_setup import setup_logging

app = typer.Typer(
    name="ai-coding",
    help="AI coding assistant CLI (ThoughtCoding Python implementation).",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        from ai_coding import __version__

        typer.echo(f"ai-coding {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """ai-coding : an OpenAI-agents powered coding assistant."""
    setup_logging()


@app.command("config-check")
def config_check(
    path: str | None = typer.Option(
        None, "--path", "-p", help="Path to a YAML config file."
    )
) -> None:
    """Load configuration and report its effective values."""
    try:
        app_cfg = ConfigLoader.load_app(path)
    except FileNotFoundError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if app_cfg.default_model:
        typer.echo(f"default_model: {app_cfg.default_model}")
    for name, m in app_cfg.models.items():
        typer.echo(
            f"  model {name}: base_url={m.base_url} "
            f"max_tokens={m.max_tokens} temperature={m.temperature}"
        )
    typer.echo(f"ai.max_context_tokens: {app_cfg.ai.max_context_tokens}")
    enabled = [name for name in ("bash", "read", "write", "edit", "glob")
               if getattr(app_cfg.tools, name).enabled]
    typer.echo(f"tools.enabled: {', '.join(enabled) or 'none'}")


@app.command("ask")
def ask(
    prompt: str = typer.Option(
        ..., "--prompt", help="The user prompt to send."
    ),
    session_id: str | None = typer.Option(
        None, "--session", "-s", help="Session id to continue; creates a new one if absent."
    ),
    path: str | None = typer.Option(None, "--path", "-p", help="Path to a YAML config file."),
) -> None:
    """Run a single turn and print the assistant's final answer."""
    try:
        manager = ConfigManager()
        manager.initialize(path)
        service = build_service_from_config(manager.app)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"ask error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.session import SessionData
    from ai_coding.service.session_service import SessionService

    sessions = SessionService()
    session: SessionData
    if session_id:
        loaded = sessions.load(session_id)
        session = loaded if loaded is not None else sessions.create()
    else:
        session = sessions.create()

    loop = AgentLoop(service, sessions)
    answer = asyncio.run(loop.process_input(session, prompt))
    typer.echo(answer)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
