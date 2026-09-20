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
    memory = MemoryService(
        MemoryStore(ws / ".memory"), config=app_cfg.memory
    )
    context = ContextManager(app_cfg.ai, transcripts_root=ws, summarizer=None)

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


def _build_tool_stack(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
) -> list[Any]:
    """Build the SDK tool list from config and CLI options.

    Pure helper — returns a list of SDK FunctionTool instances.
    """
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.bash_tool import BashTool
    from ai_coding.tools.edit_tool import EditTool
    from ai_coding.tools.glob_tool import GlobTool
    from ai_coding.tools.read_tool import ReadTool
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.sdk_adapter import registry_to_sdk_tools
    from ai_coding.tools.skill_tool import SkillTool
    from ai_coding.tools.subagent_tool import SubAgentTool
    from ai_coding.tools.todo_write_tool import TodoWriteTool
    from ai_coding.tools.write_tool import WriteTool

    sandbox = Sandbox(workspace)
    fst = FileStateTracker()
    gate = PermissionGate()
    registry = ToolRegistry()

    tools_cfg = app_cfg.tools

    if tools_cfg.bash.enabled:
        registry.register(BashTool(timeout_seconds=tools_cfg.bash.timeout_seconds))
    if tools_cfg.read.enabled:
        registry.register(
            ReadTool(sandbox, fst, max_file_size=tools_cfg.read.max_file_size)
        )
    if tools_cfg.write.enabled:
        registry.register(WriteTool(sandbox, fst))
    if tools_cfg.edit.enabled:
        registry.register(EditTool(sandbox, fst))
    if tools_cfg.glob.enabled:
        registry.register(GlobTool(sandbox))

    # Always-registered tools
    registry.register(TodoWriteTool())

    if skills_path and Path(skills_path).is_dir():
        registry.register(SkillTool(skills_path))

    # subAgent — needs ai service; for M2 we pass a stub that errors on real calls
    # (the SDK handles tool invocation, but subAgent needs its own AI service)
    # For now, register only if subagent is needed and available.
    # M3 will wire the real AI service into SubAgentTool.
    from ai_coding.ai.base import AIService

    class _DisabledSubAgent(AIService):
        async def execute_turn(
            self,
            history: list[Any],
            user_input: str,
            token_sink: Any = None,
            *,
            instructions: str | None = None,
        ) -> Any:
            from ai_coding.domain.run import TurnResult

            del history, user_input, token_sink, instructions

            return TurnResult(
                text="(sub-agent not available in M2 — M3 will enable it)"
            )

    registry.register(SubAgentTool(ai_service=_DisabledSubAgent()))

    return registry_to_sdk_tools(registry, gate, approval=None)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
