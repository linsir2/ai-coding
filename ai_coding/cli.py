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
    from ai_coding.core.guard import build_guards
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
    embeddings = _embeddings_for(app_cfg)
    memory = MemoryService(
        memory_store, extractor=extractor, config=app_cfg.memory,
        embeddings=embeddings,
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
        gate=PermissionGate.from_config(app_cfg, workspace=str(ws)),
        context=context,
        memory=memory,
        prompt=prompt,
        skills=skills_reg,
        project=project,
        settings=app_cfg.ai,
        models=app_cfg.models,
        default_model=app_cfg.default_model,
        observer=observer,
        workspace=str(ws),
        trace=trace,
        guards=guards if guards is not None else build_guards(app_cfg),
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
    # Every configured model's price, not just the default's: a fallback chain
    # bills one session through two providers, and pricing only the default
    # would silently charge the fallback's tokens at the wrong rate (G7).
    pricings: dict[str, Any] = {}
    try:
        from ai_coding.core.budget import Pricing

        for name, config in (getattr(app_cfg, "models", None) or {}).items():
            # Keyed by the *wire* name, which is what a step reports and what
            # the ledger attributes by -- the config key can differ from it
            # (``qwen`` -> ``qwen3.8-2.4t-a95b``), and keying by the wrong one
            # silently yields tokens with no price.
            pricings[str(getattr(config, "name", "") or name)] = Pricing(
                input_per_1k=float(getattr(config, "price_input_per_1k", 0.0)),
                output_per_1k=float(getattr(config, "price_output_per_1k", 0.0)),
                cached_per_1k=float(getattr(config, "price_cached_per_1k", 0.0)),
            )
    except Exception:  # noqa: BLE001
        pricings = {}
    # The old single-price attribute stays for callers that have one model.
    default_price = pricings.get(str(getattr(app_cfg, "default_model", "")))
    recorder = TraceRecorder(
        store,
        session_id=getattr(session, "session_id", "") if session is not None else "",
        redactor=Redactor(enabled=bool(getattr(tracing, "redact", True))),
        capture=str(getattr(tracing, "capture", "previews")),
    )
    recorder.pricing = default_price  # type: ignore[attr-defined]
    recorder.pricings = pricings  # type: ignore[attr-defined]
    return recorder


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


def _llm_helpers_for(service: Any) -> tuple[Any, Any]:
    """Build (summarizer, extractor) from the engine's ModelConfig when available."""
    model_config = getattr(service, "model_config", None)
    if model_config is None:
        return None, None
    from ai_coding.ai.llm_helpers import build_extractor, build_summarizer

    return build_summarizer(model_config), build_extractor(model_config)


def _embeddings_for(app_cfg: Any) -> Any | None:
    """Build the optional vector index, or None when it is off or unusable.

    Returns None (rather than a disabled index) so the service never pays for
    a capability check on the recall path when nothing is configured.
    """
    memory_cfg = getattr(app_cfg, "memory", None)
    embeddings_cfg = getattr(memory_cfg, "embeddings", None)
    if embeddings_cfg is None or not bool(getattr(embeddings_cfg, "enabled", False)):
        return None
    from ai_coding.memory.embeddings import EmbeddingIndex

    models = getattr(app_cfg, "models", {}) or {}
    return EmbeddingIndex(embeddings_cfg, models)


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
