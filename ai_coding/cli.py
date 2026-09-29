"""ai-coding entry point (Typer)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer

from ai_coding.infra.logger_setup import setup_logging

app = typer.Typer(
    name="ai-coding",
    help="AI coding assistant.",
    # R10: bare `ai-coding` starts the TUI, so it must not fall through to help.
    no_args_is_help=False,
    invoke_without_command=True,
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
    path: str | None = typer.Option(
        None, "--path", "-p", help="Path to a YAML config file."
    ),
    workspace: str = typer.Option(
        ".", "--workspace", "-w", help="Workspace directory for file tools."
    ),
    skills: str | None = typer.Option(
        None, "--skills", help="Path to skills directory (enables skill tool)."
    ),
) -> None:
    """ai-coding : an AI coding assistant."""
    setup_logging()
    if version:
        return
    from ai_coding.ui.textual_app import run_app

    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_app(path, workspace, skills))
        finally:
            loop.close()
    except (ValueError, FileNotFoundError) as exc:
        # The common case is a missing or model-less config: report it the way a
        # CLI user expects instead of a traceback.
        typer.echo(f"ai-coding: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        typer.echo("bye", err=True)


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
    trace: Any | None = None,
    guards: Any | None = None,
    hooks: Any | None = None,
    steering: Any | None = None,
    cancel: Any | None = None,
) -> Any:
    """Assemble the loop and its optional components (silent degradation)."""
    if approval is None:
        from ai_coding.security.approval import AutoApproveCallback

        approval = AutoApproveCallback()

    from ai_coding.ai.prompt import BASE_INSTRUCTIONS, PromptAssembler
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.core.project_instructions import ProjectInstructionsLoader
    from ai_coding.memory.service import MemoryService
    from ai_coding.memory.store import MemoryStore
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.skills.registry import SkillRegistry

    ws = Path(workspace).expanduser().resolve()

    skill_root = skills_path or str(ws / "skills")
    skills_reg = SkillRegistry(skill_root)

    project = ProjectInstructionsLoader(ws)
    summarizer, extractor = _llm_helpers_for(engine)
    memory_store = MemoryStore(ws / ".memory")
    memory = MemoryService(
        memory_store, extractor=extractor, config=app_cfg.memory
    )
    context = ContextManager(app_cfg.ai, transcripts_root=ws, summarizer=summarizer)

    prompt = PromptAssembler(BASE_INSTRUCTIONS)

    if memory is not None and getattr(registry, "get", None) is not None:
        if registry.get("memory_search") is None:
            from ai_coding.tools.memory_search_tool import MemorySearchTool

            registry.register(MemorySearchTool(memory))
        if registry.get("memory_write") is None:
            from ai_coding.tools.memory_write_tool import MemoryWriteTool

            registry.register(MemoryWriteTool(memory))
        if registry.get("checkpoint_write") is None:
            from ai_coding.tools.checkpoint_tool import CheckpointTool

            registry.register(CheckpointTool())

    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=approval,
        gate=PermissionGate.from_config(app_cfg),
        context=context,
        memory=memory,
        prompt=prompt,
        skills=skills_reg,
        project=project,
        settings=app_cfg.ai,
        observer=observer,
        workspace=str(ws),
        trace=trace,
        guards=guards,
        hooks=hooks,
        steering=steering,
        cancel=cancel,
    )


def _make_recorder(app_cfg: Any, workspace: str, session: Any) -> Any | None:
    """Build a trace recorder for this run, or ``None`` when tracing is off."""
    tracing = getattr(app_cfg, "tracing", None)
    if tracing is None or not getattr(tracing, "enabled", False):
        return None
    from ai_coding.core.trace import Redactor, TraceRecorder, TraceStore

    default_root = Path(workspace).expanduser().resolve() / ".ai-coding" / "traces"
    root = getattr(tracing, "dir", "") or default_root
    store = TraceStore(
        root,
        max_turn_bytes=int(getattr(tracing, "max_turn_bytes", 2_000_000)),
        retention_days=int(getattr(tracing, "retention_days", 14)),
    )
    try:
        store.prune()
    except Exception:  # noqa: BLE001
        pass
    model = getattr(app_cfg, "default_model", "")
    pricing = None
    try:
        from ai_coding.core.budget import Pricing

        config = app_cfg.models.get(model) if getattr(app_cfg, "models", None) else None
        if config is not None:
            pricing = Pricing(
                input_per_1k=float(getattr(config, "price_input_per_1k", 0.0)),
                output_per_1k=float(getattr(config, "price_output_per_1k", 0.0)),
            )
    except Exception:  # noqa: BLE001
        pricing = None
    recorder = TraceRecorder(
        store,
        session_id=getattr(session, "session_id", "") if session is not None else "",
        redactor=Redactor(enabled=bool(getattr(tracing, "redact", True))),
        capture=str(getattr(tracing, "capture", "previews")),
    )
    recorder.pricing = pricing  # type: ignore[attr-defined]
    return recorder


def _make_guards(app_cfg: Any) -> Any | None:
    """Build the guard chain from config, or ``None`` when nothing is enabled."""
    guard_cfg = getattr(app_cfg, "guard", None)
    if guard_cfg is None:
        return None
    from ai_coding.core.guard import (
        BashPolicyGuard,
        DuplicateCallGuard,
        GuardChain,
        PathEscapeGuard,
        SecretGuard,
    )

    guards: list[Any] = []
    threshold = int(getattr(guard_cfg, "duplicate_threshold", 2) or 0)
    if threshold > 0:
        guards.append(DuplicateCallGuard(threshold=threshold))
    deny = list(getattr(guard_cfg, "bash_deny", []) or [])
    confirm = list(getattr(guard_cfg, "bash_confirm", []) or [])
    if deny or confirm:
        guards.append(BashPolicyGuard(deny=deny, confirm=confirm))
    if bool(getattr(guard_cfg, "strict_paths", False)):
        guards.append(PathEscapeGuard(strict=True))
    if bool(getattr(guard_cfg, "redact_history", False)):
        guards.append(SecretGuard(redact_history=True))
    return GuardChain(guards) if guards else None


def _make_hooks(app_cfg: Any, workspace: str) -> Any | None:
    """Build the hook group from config, or ``None`` when hooks are off (R4c)."""
    config = getattr(app_cfg, "hooks", None)
    if config is None or not getattr(config, "enabled", False):
        return None
    from ai_coding.core.hooks import (
        CommandHookRunner,
        HookRunnerGroup,
        PythonHookRunner,
        specs_from_config,
    )

    specs = specs_from_config(config)
    if not specs:
        return None
    timeout = float(getattr(config, "timeout_seconds", 10.0) or 10.0)
    plugin_dir = getattr(config, "plugin_dir", "") or str(
        Path(workspace).expanduser().resolve() / ".ai-coding" / "hooks"
    )
    runners: list[Any] = []
    if any(spec.kind == "command" for spec in specs):
        runners.append(
            CommandHookRunner(specs, workspace=workspace, timeout_seconds=timeout)
        )
    if any(spec.kind == "python" for spec in specs):
        runners.append(PythonHookRunner(specs, plugin_dir=plugin_dir, workspace=workspace))
    return HookRunnerGroup(runners) if runners else None


def _compose_observer(base: Any, recorder: Any) -> Any:
    """Braid the UI observer with the trace recorder (either may be absent)."""
    if recorder is None:
        return base
    from ai_coding.core.observers import Braid

    return Braid(*([base] if base is not None else []), recorder)


def _llm_helpers_for(service: Any) -> tuple[Any, Any]:
    """Build (summarizer, extractor) from the engine's ModelConfig when available."""
    model_config = getattr(service, "model_config", None)
    if model_config is None:
        return None, None
    from ai_coding.ai.llm_helpers import build_extractor, build_summarizer

    return build_summarizer(model_config), build_extractor(model_config)


def _build_tool_registry(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    *,
    user_prompt: Any | None = None,
    base_instructions: str = "",
) -> Any:
    """Build the domain tool registry the self-owned loop dispatches through."""
    from ai_coding.tools.builder import build_tool_registry

    return build_tool_registry(
        app_cfg,
        workspace,
        skills_path,
        include_subagent=True,
        user_prompt=user_prompt,
        base_instructions=base_instructions,
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
