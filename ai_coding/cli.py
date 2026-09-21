"""ai-coding entry point (Typer)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer

from ai_coding.ai.factory import build_service_from_config
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

        # Build the tool stack
        sdk_tools = _build_tool_stack(app_cfg, workspace, skills)

        service = build_service_from_config(app_cfg, tools=sdk_tools)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"ask error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    from ai_coding.domain.session import SessionData
    from ai_coding.service.session_service import SessionService

    sessions = SessionService()
    session: SessionData
    if session_id:
        loaded = sessions.load(session_id)
        session = loaded if loaded is not None else sessions.create()
    else:
        session = sessions.create()

    def on_delta(token: str) -> None:
        typer.echo(token, nl=False)

    loop = _build_loop(service, sessions, app_cfg, workspace, skills)
    answer = asyncio.run(loop.process_input(session, prompt, on_delta=on_delta))
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
    from ai_coding.service.session_service import SessionService
    from ai_coding.ui.console import DialogueConsole
    from ai_coding.ui.repl import run_repl

    try:
        manager = ConfigManager()
        manager.initialize(path)
        app_cfg = manager.app

        # Interactive REPL: confirm WARN-tier tools (write/edit/bash) with the
        # user via y/N/s prompts, rather than silently blocking or auto-approving.
        from ai_coding.security.approval import CLIApprovalCallback

        sdk_tools = _build_tool_stack(
            app_cfg, workspace, skills, approval=CLIApprovalCallback()
        )
        service = build_service_from_config(app_cfg, tools=sdk_tools)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"repl error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    sessions = SessionService()
    loop = _build_loop(service, sessions, app_cfg, workspace, skills)
    asyncio.run(run_repl(loop, sessions, DialogueConsole()))


def _build_loop(
    service: Any,
    sessions: Any,
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
) -> Any:
    """Assemble the M3 components (all optional; silent degradation when missing)."""
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
    # M3 integration: wire the real LLM helpers (L4 summarizer + memory
    # extractor) when the service exposes a ModelConfig; fake services keep the
    # offline defaults so unit tests never touch the network.
    summarizer, extractor = _llm_helpers_for(service)
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
        service,
        sessions,
        context=context,
        memory=memory,
        prompt=prompt,
        skills=skills_reg,
        project=project,
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


def _build_tool_stack(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    approval: Any | None = None,
) -> list[Any]:
    """Build the SDK tool list from config and CLI options.

    Delegates to the shared :mod:`ai_coding.tools.builder` so the main agent and
    each sub-agent assemble identical stacks. ``subAgent`` is wired to a real
    :class:`SubAgentRunner` (worktree-isolated per ``ai.subagent_worktree_isolation``).
    ``approval`` resolves the WARN tier (write/edit/bash); ``None`` auto-approves.
    """
    from ai_coding.tools.builder import build_tool_stack

    return build_tool_stack(
        app_cfg, workspace, skills_path, include_subagent=True, approval=approval
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
