"""ai-coding entry point (Typer)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import typer

from ai_coding.config.models import resolve_context_window
from ai_coding.infra.logger_setup import send_logs_to_file, setup_logging
from ai_coding.paths import Paths, config_home
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.skills.registry import SkillRegistry

_log = logging.getLogger(__name__)

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
    ctx: typer.Context,
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
    if ctx.invoked_subcommand is not None:
        # A subcommand owns the invocation. Falling through here would launch
        # the TUI underneath `ai-coding skill list`.
        return
    setup_logging()
    if version:
        return
    # The TUI owns the terminal from here: a log record written to stderr lands
    # in the middle of the frame, and the renderer repaints only the regions it
    # believes changed, so fragments of it stay on screen.  Logs go to the
    # workspace's .ai-coding/ instead.
    send_logs_to_file(workspace)
    from ai_coding.server.launcher import run_tui

    runner = run_tui

    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(runner(path, workspace, skills))
        finally:
            loop.close()
    except (ValueError, FileNotFoundError) as exc:
        # The common case is a missing or model-less config: report it the way a
        # CLI user expects instead of a traceback.
        typer.echo(f"ai-coding: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        typer.echo("bye", err=True)


def _extra_roots(app_cfg: Any, workspace: str) -> tuple[str, ...]:
    """The operator's declared roots, expanded (B/D5-c).

    Shared with the tool stack so the sandbox that enforces a path and the
    warning that describes one are looking at the same set -- they used to
    disagree, and the warning was the half that was wrong.
    """
    from ai_coding.tools.builder import expand_root_patterns

    configured = getattr(getattr(app_cfg, "tools", None), "extra_workspace_roots", ())
    base = Path(workspace).expanduser().resolve()
    return tuple(str(path) for path in expand_root_patterns(configured, base=base))


def _build_gate(app_cfg: Any, workspace: str, skills: SkillRegistry) -> PermissionGate:
    """The permission gate with the skill policy wired (E-5).

    The resolver is the registry: ``ai.skill_permissions`` keys and the tool's
    ``name`` argument must meet in one namespace even when a skill is
    addressed by its declared alias.
    """

    def resolve(name: str) -> str | None:
        skill = skills.resolve(name)
        return skill.id if skill is not None else None

    return PermissionGate.from_config(app_cfg, workspace=workspace, skill_resolver=resolve)


def _build_loop(
    engine: Any,
    sessions: Any,
    registry: Any,
    app_cfg: Any,
    workspace: str,
    skills: SkillRegistry,
    paths: Paths | None = None,
    *,
    approval: Any | None = None,
    observer: Any | None = None,
    trace: Any | None = None,
    guards: Any | None = None,
    hooks: Any | None = None,
    notices: Any | None = None,
    jobs: Any | None = None,
    cancel: Any | None = None,
    # M2: per-step MCP catalog text; ``None`` in paths without an MCP manager
    # (the bare CLI), where there is nothing to catalog.
    catalog_builder: Any | None = None,
    # r17/T1: pre-turn workspace snapshots.  ``None`` keeps /undo to the
    # history half (the bare-CLI paths that never wire one).
    snapshots: Any | None = None,
    # M1b: the agent table.  ``None`` keeps the loop without an agent layer,
    # which is what every test double and the bare REPL want.
    agents: Any | None = None,
    # E.2: the gate the **sandbox** already asks.  Passing it in is what makes a
    # runtime "always" reach both halves of the boundary -- the dispatcher
    # records the grant on this object and ``Sandbox.resolve``/``live_roots``
    # read it back.  Two gates would mean the operator's answer lands in one
    # and the file tools keep refusing it.
    gate: Any | None = None,
    # R2: the composition root's mutable holder for the *current* model key.
    # The loop writes it in ``set_model``; the sub-agent engine factory reads
    # it.  ``None`` (tests, the bare REPL) keeps the factory on the configured
    # default, exactly as before.
    current_key: list[str] | None = None,
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
    from ai_coding.memory.scoped_store import ScopedStore
    from ai_coding.memory.service import MemoryService
    from ai_coding.memory.store import MemoryStore

    ws = Path(workspace).expanduser().resolve()
    paths = paths or Paths(workspace=ws)

    project = ProjectInstructionsLoader(ws)
    summarizer, extractor = _llm_helpers_for(engine)
    # Two tiers (M10): the project's own file, and the one that follows the
    # person to every project. A user file that does not exist contributes
    # nothing, so a machine that has never written one behaves exactly as before.
    memory_store = ScopedStore(MemoryStore(paths.memory), MemoryStore(paths.user_memory))
    embeddings = _embeddings_for(app_cfg, paths)
    memory = MemoryService(
        memory_store, extractor=extractor, config=app_cfg.memory,
        embeddings=embeddings,
    )
    effective_window = resolve_context_window(app_cfg)
    # A budget far below the model's own window is an operator choice, not a
    # bug -- but it makes compaction fire at that budget, not near the model's
    # window.  Say so once: "context 62% used" beside a 48k ceiling is exactly
    # how a run looks broken when it is only configured small (trace 2026-10-31).
    declared = 0
    models = getattr(app_cfg, "models", None) or {}
    if isinstance(models, dict):
        model = models.get(str(getattr(app_cfg, "default_model", "") or ""))
    else:  # pragma: no cover — a hand-built object, not the loaded config
        model = getattr(models, str(getattr(app_cfg, "default_model", "") or ""), None)
    if isinstance(model, dict):
        raw_window = model.get("context_window", 0)
    else:
        raw_window = getattr(model, "context_window", 0)
    try:
        declared = max(0, int(raw_window or 0))
    except (TypeError, ValueError):
        declared = 0
    configured = int(getattr(app_cfg.ai, "max_context_tokens", 0) or 0)
    if configured > 0 and declared > 2 * configured:
        _log.warning(
            "ai.max_context_tokens=%d is far below model %r's declared window %d; "
            "compaction will fire near %d, not the model window. Set it to 0 "
            "(or the model window) to compact near the real limit.",
            configured,
            str(getattr(app_cfg, "default_model", "") or ""),
            declared,
            configured,
        )
    context = ContextManager(
        app_cfg.ai,
        transcripts_root=ws,
        summarizer=summarizer,
        context_window=effective_window,
    )

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
        # These two joined the registry after ``build_tool_registry`` ran its
        # defer pass; applying the same composition decision here keeps their
        # schemas off the request until ``tool_search`` asks for them.
        from ai_coding.tools.builder import mark_deferred_tools

        mark_deferred_tools(registry)

    if snapshots is None:
        # Every interactive entry point gets undo; only the history half works
        # outside a git repo (SnapshotStore degrades there by design).
        from ai_coding.core.snapshots import SnapshotStore

        snapshots = SnapshotStore(ws)
    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=approval,
        gate=gate if gate is not None else _build_gate(app_cfg, str(ws), skills),
        snapshots=snapshots,
        context=context,
        memory=memory,
        prompt=prompt,
        catalog_builder=catalog_builder,
        skills=skills,
        project=project,
        settings=app_cfg.ai,
        agents=agents,
        models=app_cfg.models,
        default_model=app_cfg.default_model,
        observer=observer,
        workspace=str(ws),
        trace=trace,
        guards=guards if guards is not None else build_guards(app_cfg),
        hooks=hooks,
        notices=notices,
        jobs=jobs,
        cancel=cancel,
        extra_workspace_roots=_extra_roots(app_cfg, str(ws)),
        current_key=current_key,
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
        workspace=Path(workspace).expanduser().resolve(),
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
    # The default lives at the user tier, not in the repo tree: a python hook is
    # ``exec_module``'d, so a default inside the workspace would let the
    # repository supply the very module the operator's own config then runs.
    # An explicit ``plugin_dir`` (user layer) still wins.
    plugin_dir = getattr(config, "plugin_dir", "") or str(config_home() / "hooks")
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


def _embeddings_for(app_cfg: Any, paths: Paths | None = None) -> Any | None:
    """Build the optional vector index, or None when it is off or unusable.

    Returns None (rather than a disabled index) so the service never pays for
    a capability check on the recall path when nothing is configured.

    The cache path defaults next to the memory it indexes. It used to default
    to the bare string ``.memory/embeddings.json``, resolved against the
    *process* CWD — so an unrelated ``cd`` orphaned the vector index and two
    projects started from the same directory shared one.
    """
    memory_cfg = getattr(app_cfg, "memory", None)
    embeddings_cfg = getattr(memory_cfg, "embeddings", None)
    if embeddings_cfg is None or not bool(getattr(embeddings_cfg, "enabled", False)):
        return None
    from ai_coding.memory.embeddings import EmbeddingIndex

    models = getattr(app_cfg, "models", {}) or {}
    if paths is None:
        return EmbeddingIndex(embeddings_cfg, models)
    configured = str(getattr(embeddings_cfg, "cache_path", "") or "").strip()
    target = Path(configured).expanduser() if configured else paths.memory / "embeddings.json"
    if not target.is_absolute():
        target = paths.workspace / target
    # The cache is a pure accelerator, so the guard falls back to the default
    # rather than refusing the feature.  It applies to *every* layer, not just
    # the project one: a future caller may hand an untrusted path in without
    # knowing where it came from.  ``resolve()`` first, because a symlink inside
    # the workspace that points out would otherwise pass a lexical check.
    default_target = paths.memory / "embeddings.json"
    try:
        resolved = target.resolve()
        workspace = paths.workspace.resolve()
    except OSError:
        resolved, workspace = target, paths.workspace
    if not resolved.is_relative_to(workspace):
        _log.warning(
            "embedding cache path %s is outside the workspace; using %s instead",
            target,
            default_target,
        )
        target = default_target
    return EmbeddingIndex(embeddings_cfg, models, cache_path=target)


def _build_tool_registry(
    app_cfg: Any,
    workspace: str,
    skills: SkillRegistry,
    *,
    user_prompt: Any | None = None,
    base_instructions: str = "",
    notices: Any | None = None,
    jobs: Any | None = None,
    agents: Any | None = None,
    sandbox_policy: Any = None,
    sandbox_backend: Any = None,
    sandbox_roots: Any = (),
    gate: Any = None,
    sandbox: Any = None,
    current_key: list[str] | None = None,
) -> Any:
    """Build the domain tool registry the self-owned loop dispatches through."""
    from ai_coding.tools.builder import build_tool_registry

    return build_tool_registry(
        app_cfg,
        workspace,
        skills,
        include_subagent=True,
        user_prompt=user_prompt,
        base_instructions=base_instructions,
        notices=notices,
        jobs=jobs,
        agents=agents,
        sandbox_policy=sandbox_policy,
        sandbox_backend=sandbox_backend,
        sandbox_roots=sandbox_roots,
        gate=gate,
        sandbox=sandbox,
        current_key=current_key,
    )


# ---- ai-coding skill ------------------------------------------------------
#
# Kept in this module so the subcommand lives beside the options it shares
# (`-w`), rather than in a second Typer app that would have to be dispatched by
# hand and could drift.


def _library(workspace: str, *, global_scope: bool) -> Path:
    """Where a command reads and writes.

    Default is the project's own library, because a skill you install while
    working on a repo belongs to that repo. `--global` says otherwise
    deliberately, rather than by accident of which directory you are in.
    """
    from ai_coding.paths import Paths

    paths = Paths.for_workspace(workspace)
    return paths.user_skills if global_scope else paths.project_skills


skill_app = typer.Typer(
    help="Install, list and remove skills.", no_args_is_help=True
)


@skill_app.command("install")
def skill_install(
    source: str = typer.Argument(..., help="A local directory, or owner/repo."),
    workspace: str = typer.Option(".", "--workspace", "-w"),
    global_scope: bool = typer.Option(
        False, "--global", help="Install into the user library instead of the project's."
    ),
    force: bool = typer.Option(False, "--force", help="Replace a skill of the same name."),
) -> None:
    """Install one skill, or every skill in a directory or repository."""
    import asyncio

    from ai_coding.paths import Paths
    from ai_coding.skills.install import InstallError, install_from_git, install_from_path

    library = _library(workspace, global_scope=global_scope)
    library.mkdir(parents=True, exist_ok=True)
    try:
        looks_remote = "://" in source or source.count("/") == 1
        if looks_remote:
            result = asyncio.run(install_from_git(source, library, force=force))
        else:
            result = install_from_path(Path(source).expanduser(), library, force=force)
    except InstallError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    where = Paths.for_workspace(workspace).user_skills if global_scope else library
    for name in result.installed:
        typer.echo(f"installed {name}  →  {where}")
    for name in result.replaced:
        typer.echo(f"replaced {name}  →  {where}")
    if not result.changed:
        typer.echo("nothing to do")


@skill_app.command("list")
def skill_list(
    workspace: str = typer.Option(".", "--workspace", "-w"),
    global_scope: bool = typer.Option(False, "--global"),
) -> None:
    """List the installed skills and where each description came from."""
    from ai_coding.skills.install import list_installed

    rows = list_installed(_library(workspace, global_scope=global_scope))
    if not rows:
        typer.echo("(no skills installed here)")
        return
    for row in rows:
        typer.echo(f"  {row['name']}  {row['description']}  [{row['status']}]")


@skill_app.command("remove")
def skill_remove(
    name: str = typer.Argument(..., help="Skill to remove."),
    workspace: str = typer.Option(".", "--workspace", "-w"),
    global_scope: bool = typer.Option(False, "--global"),
) -> None:
    """Remove an installed skill. There is no undo."""
    from ai_coding.skills.install import remove_skill

    library = _library(workspace, global_scope=global_scope)
    if not remove_skill(library, name):
        typer.secho(f"error: no skill named {name} in {library}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"removed {name}")


app.add_typer(skill_app, name="skill")


# ---- ai-coding trace ------------------------------------------------------
#
# `render_overview` (R9b) and `build_report` survived R10, but the command that
# called them did not, so the renderer sat in `__all__` with no consumer.  This
# is that command again, and `--since` gets its first production caller.


trace_app = typer.Typer(help="Inspect traced sessions.", no_args_is_help=True)


@trace_app.command("overview")
def trace_overview(
    workspace: str = typer.Option(".", "--workspace", "-w"),
    since: int = typer.Option(
        0,
        "--since",
        min=0,
        help="Only sessions active in the last N days (0 = all). This is a view "
        "window, not the delete threshold -- deletion is tracing.retention_days.",
    ),
    output_format: str = typer.Option("table", "--format", help="table | json"),
) -> None:
    """One table for every traced session in this workspace (R9b, restored in M-G2).

    ``render_overview`` already existed and was exported; R10 deleted the command
    that called it and left it with no consumer.  Reusing it here rather than
    writing a second renderer is the point -- a copy would drift.
    """
    import json
    from dataclasses import asdict

    from ai_coding.app.trace_text import render_overview
    from ai_coding.core.aggregate import build_report
    from ai_coding.core.trace import TraceStore

    if output_format not in ("table", "json"):
        typer.secho(
            f"error: --format must be table or json, got {output_format!r}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    ws = Path(workspace).expanduser().resolve()
    root = ws / ".ai-coding" / "traces"
    report = build_report(TraceStore(root, workspace=ws), since_days=since)
    if output_format == "json":
        # An empty store is still a JSON document: `--format json | jq .sessions`
        # must not have to special-case prose.
        typer.echo(
            json.dumps(
                {
                    "sessions": [
                        {
                            "session": row.session,
                            "turns": row.turns,
                            "files": row.files,
                            "steps": row.steps,
                            "prompt_tokens": row.prompt_tokens,
                            "completion_tokens": row.completion_tokens,
                            "cached_tokens": row.cached_tokens,
                            "cache_ratio": row.cache_ratio,
                            "age_days": row.age_days,
                            "mismatch": row.mismatch,
                        }
                        for row in report.rows
                    ],
                    "totals": asdict(report.totals),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not report.rows:
        typer.echo(f"(no traced sessions under {root})")
        return
    typer.echo(render_overview(report))


app.add_typer(trace_app, name="trace")


# ---- ai-coding config -----------------------------------------------------
#
# Where the user layer lives, and how to create it.  The trust filter means a
# *project* config may not carry a model's endpoint or key (a repository must not
# choose where your code goes), so the user layer is the only place a key can
# live -- and until M-H2 there was no way to create it except by hand at a path
# the docs never named.

_CONFIG_TEMPLATE = """\
# ai-coding user configuration.
#
# This file is the only layer allowed to carry model endpoints and keys: a
# project's config may tune behaviour, but it may not decide where a request
# goes or which credential it carries.
#
# The key is read from the environment, so it is not written here.  Export it:
#     export DEEPSEEK_API_KEY=sk-...
# ... or replace the reference below with a literal (less safe).

models:
  deepseek-chat:
    provider: deepseek            # fills base_url from the provider table
    name: deepseek-chat
    api_key: ${DEEPSEEK_API_KEY}
default_model: deepseek-chat

# Other providers, same shape -- uncomment one and point default_model at it:
#   gpt-5:
#     provider: openai
#     name: gpt-5
#     api_key: ${OPENAI_API_KEY}
#   qwen-max:
#     provider: qwen
#     name: qwen-max
#     api_key: ${DASHSCOPE_API_KEY}
#   llama-local:
#     provider: ollama            # no key: a local server
#     name: llama3
"""


config_app = typer.Typer(help="Where configuration lives.", no_args_is_help=True)


@config_app.command("path")
def config_path() -> None:
    """Print the user-layer config file (it may not exist yet)."""
    from ai_coding.paths import config_home

    typer.echo(str(config_home() / "config.yaml"))


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a starter user-layer config at 0600, creating the directory at 0700.

    ``O_EXCL`` rather than "write then chmod": with the latter the file exists
    world-readable between the two calls, and umask decides what lands there.
    """
    import os

    from ai_coding.paths import config_home

    directory = config_home()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover - a shared/odd filesystem
        pass

    path = directory / "config.yaml"
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        typer.secho(
            f"error: {path} already exists; pass --force to overwrite it",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    # `O_TRUNC` keeps the existing file's mode, so an old 0644 file would stay
    # readable after a --force rewrite.  `O_EXCL` already created it at 0600.
    if force:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)  # nothing will own the descriptor otherwise
            raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(_CONFIG_TEMPLATE)

    typer.echo(f"wrote {path} (0600)")
    typer.echo(
        "next: export DEEPSEEK_API_KEY=..., or edit api_key in that file. "
        "If this workspace has an older config.local.yaml with a literal key, "
        "delete it or replace the key with ${DEEPSEEK_API_KEY}."
    )


app.add_typer(config_app, name="config")


def run() -> None:
    app()


if __name__ == "__main__":
    run()
