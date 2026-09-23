"""ai-coding entry point (Typer)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer

from ai_coding.ai.factory import build_engine_from_config
from ai_coding.config.loader import ConfigLoader, ConfigManager
from ai_coding.infra.logger_setup import setup_logging

app = typer.Typer(
    name="ai-coding",
    help="AI coding assistant CLI.",
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
    workspace: str = typer.Option(
        ".", "--workspace", "-w", help="Workspace directory for file tools."
    ),
    skills: str | None = typer.Option(
        None, "--skills", help="Path to skills directory (enables skill tool)."
    ),
) -> None:
    """Run a single turn with tools and print the assistant's final answer."""
    try:
        manager = ConfigManager()
        manager.initialize(path)
        app_cfg = manager.app
        engine = build_engine_from_config(app_cfg)
        registry = _build_tool_registry(app_cfg, workspace, skills)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"ask error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _warn_if_mcp_enabled(app_cfg)

    from ai_coding.domain.session import SessionData
    from ai_coding.service.session_service import SessionService
    from ai_coding.ui.console import ConsoleObserver, DialogueConsole

    sessions = SessionService()
    session: SessionData
    if session_id:
        loaded = sessions.load(session_id)
        session = loaded if loaded is not None else sessions.create()
    else:
        session = sessions.create()

    def on_delta(token: str) -> None:
        typer.echo(token, nl=False)

    console = DialogueConsole()
    loop = _build_loop(
        engine,
        sessions,
        registry,
        app_cfg,
        workspace,
        skills,
        observer=ConsoleObserver(console),
    )
    answer = asyncio.run(_run_turn(loop, session, prompt, on_delta))
    # Answer text was already streamed live via on_delta; only ensure a trailing newline.
    if answer and not answer.endswith("\n"):
        typer.echo()


@app.command("repl")
def repl(
    path: str | None = typer.Option(None, "--path", "-p", help="Path to a YAML config file."),
    workspace: str = typer.Option(
        ".", "--workspace", "-w", help="Workspace directory for file tools."
    ),
    skills: str | None = typer.Option(
        None, "--skills", help="Path to skills directory (enables skill tool)."
    ),
) -> None:
    """Start an interactive REPL session (slash commands: /help /status /clear /quit)."""
    from ai_coding.security.approval import CLIApprovalCallback
    from ai_coding.service.session_service import SessionService
    from ai_coding.ui.console import ConsoleObserver, DialogueConsole
    from ai_coding.ui.repl import run_repl

    try:
        manager = ConfigManager()
        manager.initialize(path)
        app_cfg = manager.app
        engine = build_engine_from_config(app_cfg)
        registry = _build_tool_registry(app_cfg, workspace, skills)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"repl error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _warn_if_mcp_enabled(app_cfg)

    sessions = SessionService()
    console = DialogueConsole()
    # Interactive REPL: confirm WARN-tier tools (write/edit/bash) with the user
    # via y/N/s prompts, rather than silently blocking or auto-approving.
    loop = _build_loop(
        engine,
        sessions,
        registry,
        app_cfg,
        workspace,
        skills,
        approval=CLIApprovalCallback(),
        observer=ConsoleObserver(console),
    )

    async def _serve() -> None:
        try:
            await run_repl(loop, sessions, console)
        except KeyboardInterrupt:
            console.info("[aborted]")
        finally:
            await loop.aclose()

    asyncio.run(_serve())


def _build_loop(
    engine: Any,
    sessions: Any,
    registry: Any,
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    *,
    approval: Any | None = None,
    observer: Any | None = None,
) -> Any:
    """Assemble the loop and its optional components (silent degradation)."""
    from ai_coding.ai.prompt import PromptAssembler
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.core.project_instructions import ProjectInstructionsLoader
    from ai_coding.memory.service import MemoryService
    from ai_coding.memory.store import MemoryStore
    from ai_coding.skills.registry import SkillRegistry

    ws = Path(workspace).expanduser().resolve()

    # Skills registry — falls back to a missing-dir-safe empty registry.
    skill_root = skills_path or str(ws / "skills")
    skills_reg = SkillRegistry(skill_root)

    # Project instructions loader + memory live under the workspace.
    project = ProjectInstructionsLoader(ws)
    # Wire the real LLM helpers (L4 summarizer + memory extractor) when the
    # engine exposes a ModelConfig; fakes keep the offline defaults so unit tests
    # never touch the network.
    summarizer, extractor = _llm_helpers_for(engine)
    memory_store = MemoryStore(ws / ".memory")
    memory = MemoryService(
        memory_store, extractor=extractor, config=app_cfg.memory
    )
    context = ContextManager(app_cfg.ai, transcripts_root=ws, summarizer=summarizer)

    base_instructions = (
        "You are an AI coding assistant. Respond to the user's coding questions "
        "clearly and concisely, in the same language the user writes in."
    )
    prompt = PromptAssembler(base_instructions)

    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=approval,
        context=context,
        memory=memory,
        prompt=prompt,
        skills=skills_reg,
        project=project,
        settings=app_cfg.ai,
        observer=observer,
        workspace=str(ws),
    )


async def _run_turn(loop: Any, session: Any, prompt: str, on_delta: Any) -> str:
    """Run one turn and always release the engine's HTTP client."""
    try:
        return str(await loop.process_input(session, prompt, on_delta=on_delta))
    finally:
        await loop.aclose()


def _warn_if_mcp_enabled(app_cfg: Any) -> None:
    """Say so loudly when MCP is configured: this build ignores it."""
    if getattr(getattr(app_cfg, "mcp", None), "enabled", False):
        typer.echo(
            "warning: MCP is enabled in your config, but this build runs the "
            "self-owned loop which does not support MCP yet — the configured "
            "servers are ignored.",
            err=True,
        )


def _llm_helpers_for(service: Any) -> tuple[Any, Any]:
    """Build (summarizer, extractor) from the engine's ModelConfig when available.

    Returns ``(None, None)`` for services without a ``model_config`` (e.g. unit
    test fakes) so the offline defaults stay in place and nothing touches the
    network outside of a real CLI run.
    """
    model_config = getattr(service, "model_config", None)
    if model_config is None:
        return None, None
    from ai_coding.ai.llm_helpers import build_extractor, build_summarizer

    return build_summarizer(model_config), build_extractor(model_config)


def _build_tool_registry(
    app_cfg: Any, workspace: str, skills_path: str | None
) -> Any:
    """Build the domain tool registry the self-owned loop dispatches through.

    Permission and approval are *not* part of the registry: they belong to the
    ``ToolDispatcher`` the loop creates.
    """
    from ai_coding.tools.builder import build_tool_registry

    return build_tool_registry(
        app_cfg, workspace, skills_path, include_subagent=True
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
