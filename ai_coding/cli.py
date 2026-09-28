"""ai-coding entry point (Typer)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from ai_coding.ai.factory import build_engine_from_config
from ai_coding.config.loader import ConfigLoader, ConfigManager
from ai_coding.core.mcp import MCPManager
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
    # R4: what the observability, guard and hook layers will actually do.
    tracing = app_cfg.tracing
    typer.echo(
        f"tracing: enabled={tracing.enabled} capture={tracing.capture} "
        f"retention_days={tracing.retention_days}"
    )
    guards = app_cfg.guard
    guard_names = []
    if guards.duplicate_threshold > 0:
        guard_names.append(f"duplicate_call({guards.duplicate_threshold})")
    if guards.bash_deny or guards.bash_confirm:
        guard_names.append("bash_policy")
    if guards.strict_paths:
        guard_names.append("path_escape")
    if guards.redact_history:
        guard_names.append("secret(history)")
    typer.echo(f"guard.active: {', '.join(guard_names) or 'none'}")
    hooks = app_cfg.hooks
    typer.echo(
        f"hooks: enabled={hooks.enabled} "
        f"events={len(hooks.events)} timeout={hooks.timeout_seconds:g}s"
    )
    typer.echo(f"ai.turn_token_budget: {app_cfg.ai.turn_token_budget}")


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
        # R7e-4: the same base instructions a task_only sub-agent would get.
        from ai_coding.ai.prompt import BASE_INSTRUCTIONS

        registry = _build_tool_registry(
            app_cfg, workspace, skills, base_instructions=BASE_INSTRUCTIONS
        )
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"ask error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

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
    recorder = _make_recorder(app_cfg, workspace, session)
    guards = _make_guards(app_cfg)
    hooks = _make_hooks(app_cfg, workspace)
    answer = asyncio.run(
        _run_turn_with_mcp(
            engine=engine,
            app_cfg=app_cfg,
            registry=registry,
            sessions=sessions,
            session=session,
            prompt=prompt,
            on_delta=on_delta,
            workspace=workspace,
            skills_path=skills,
            observer=_compose_observer(
                ConsoleObserver(
                    console, chunks=bool(getattr(app_cfg.ui, "tool_chunks", True))
                ),
                recorder,
            ),
            trace=recorder,
            guards=guards,
            hooks=hooks,
        )
    )
    # Answer text was already streamed live via on_delta; only ensure a trailing newline.
    if answer and not answer.endswith("\n"):
        typer.echo()


@app.command("memory")
def memory_command(
    action: str = typer.Argument("list", help="list | search | forget | pin"),
    query: str = typer.Argument("", help="Search text, or a note id for pin/forget."),
    path: str | None = typer.Option(None, "--path", "-p", help="Path to a YAML config file."),
    workspace: str = typer.Option(
        ".", "--workspace", "-w", help="Workspace directory (holds .memory)."
    ),
) -> None:
    """Inspect and curate the durable memory index (R3c)."""
    from ai_coding.memory.service import MemoryService
    from ai_coding.memory.store import MemoryStore

    try:
        app_cfg = ConfigLoader.load_app(path)
    except FileNotFoundError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    ws = Path(workspace).expanduser().resolve()
    store = MemoryStore(ws / ".memory")
    service = MemoryService(store, config=app_cfg.memory)

    if action == "list":
        notes = store.all()
        if not notes:
            typer.echo("(empty)")
        for note in notes:
            flags = "pinned " if note.pinned else ""
            state = "" if note.is_active else "superseded "
            typer.echo(
                f"{note.id[:8]}  [{flags}{state}{note.kind.value}] hits={note.hits} "
                f"{note.text}"
            )
        return

    if action == "search":
        hits = asyncio.run(service.recall_notes(query, k=10)) if query else []
        if not hits:
            typer.echo("(no matches)")
        for hit in hits:
            typer.echo(
                f"{hit.note.id[:8]}  [{hit.note.kind.value}] score={hit.score:.2f} "
                f"rel={hit.relevance:.2f} {hit.note.text}"
            )
        return

    if action in {"forget", "pin"}:
        if not query:
            typer.echo(f"usage: memory {action} <note-id>", err=True)
            raise typer.Exit(code=1)
        target = next((n for n in store.all() if n.id.startswith(query)), None)
        if target is None:
            typer.echo(f"no note matching '{query}'", err=True)
            raise typer.Exit(code=1)
        if action == "forget":
            store.delete(target.id)
            typer.echo(f"forgot {target.id[:8]}: {target.text}")
        else:
            updated = store.pin(target.id, not target.pinned)
            state = "pinned" if updated and updated.pinned else "unpinned"
            typer.echo(f"{state} {target.id[:8]}: {target.text}")
        return

    typer.echo(f"unknown action '{action}'", err=True)
    raise typer.Exit(code=1)


@app.command("trace")
def trace_command(
    action: str = typer.Argument("list", help="list | show | budget | overview"),
    session: str = typer.Argument("", help="Session id, or a turn file path."),
    turn: int = typer.Option(0, "--turn", "-t", help="Turn number for `show` (0 = latest)."),
    workspace: str = typer.Option(
        ".", "--workspace", "-w", help="Workspace directory (holds .ai-coding/traces)."
    ),
    path: str | None = typer.Option(None, "--path", "-p", help="Path to a YAML config file."),
    since: int = typer.Option(
        0, "--since", help="Only sessions touched in the last N days (0 = all)."
    ),
    fmt: str = typer.Option("table", "--format", help="table | json | html | otlp-json"),
    session_id: str = typer.Option(
        "", "--session", "-s", help="Session to export (defaults to the positional one)."
    ),
    all_sessions: bool = typer.Option(False, "--all", help="Export every session."),
    out: str = typer.Option("", "--out", "-o", help="Write to this file instead of stdout."),
    full: bool = typer.Option(False, "--full", help="Include the recorded message payloads."),
) -> None:
    """Inspect recorded traces and token totals (R4a)."""
    from ai_coding.ui.trace_render import render_budget, render_turn, summarize_turn

    try:
        app_cfg = ConfigLoader.load_app(path)
    except FileNotFoundError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    root_cfg = getattr(app_cfg.tracing, "dir", "") if app_cfg.tracing else ""
    default_root = Path(workspace).expanduser().resolve() / ".ai-coding" / "traces"
    root = Path(root_cfg) if root_cfg else default_root

    if action == "list":
        if not Path(root).is_dir():
            typer.echo("(no traces recorded yet)")
            return
        for entry in sorted(Path(root).iterdir()):
            if not entry.is_dir():
                continue
            turns = sorted(entry.glob("turn-*.jsonl"))
            typer.echo(f"{entry.name}  {len(turns)} turn(s)")
        return

    if action == "overview":
        from dataclasses import asdict

        from ai_coding.core.aggregate import build_report
        from ai_coding.core.trace import TraceStore
        from ai_coding.ui.trace_render import render_overview

        report = build_report(TraceStore(root), since_days=max(0, int(since)))
        if not report.rows:
            typer.echo("(no traces recorded yet)")
            return
        if fmt == "json":
            payload = {
                "rows": [
                    {**asdict(row), "mismatch": row.mismatch, "cache_ratio": row.cache_ratio}
                    for row in report.rows
                ],
                "totals": {
                    **asdict(report.totals),
                    "mismatch": report.totals.mismatch,
                    "cache_ratio": report.cache_ratio,
                },
            }
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        typer.echo(render_overview(report))
        return

    if action == "export":
        from ai_coding.core.aggregate import build_report
        from ai_coding.core.export_html import (
            read_session_view,
            render_index_html,
            render_session_html,
        )
        from ai_coding.core.trace import TraceStore

        session = session_id or session
        store = TraceStore(root)
        if all_sessions:
            views = [read_session_view(store, ref.session, full=full)
                     for ref in store.sessions()]
            if not views:
                typer.echo("(no traces recorded yet)")
                return
            body = render_index_html(build_report(store), views)
        else:
            directory = store.session_dir(session)
            if not session or not directory.is_dir():
                typer.echo(f"error: no traces for session '{session or '(none)'}'")
                raise typer.Exit(code=1)
            views = [read_session_view(store, session, full=full)]
            body = render_session_html(views[0])
        if out:
            Path(out).write_text(body, encoding="utf-8")
            typer.echo(f"wrote {out}")
            return
        typer.echo(body)
        return

    if not session:
        typer.echo(f"usage: trace {action} <session-id>", err=True)
        raise typer.Exit(code=1)

    session_dir = Path(root) / session
    if action == "budget":
        summary_path = session_dir / "summary.json"
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            typer.echo(render_budget(summary))
        else:
            typer.echo("(no recorded usage for this session)")
        return

    if action == "show":
        candidates = sorted(session_dir.glob("turn-*.jsonl"))
        if not candidates:
            typer.echo(f"(no traces for session {session})")
            return
        target = next((c for c in candidates if c.stem == f"turn-{turn}"), candidates[-1])
        typer.echo(f"{target}")
        typer.echo(render_turn(summarize_turn(target)))
        return

    typer.echo(f"unknown action '{action}'", err=True)
    raise typer.Exit(code=1)


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
    from ai_coding.core.steering import SteeringQueue
    from ai_coding.security.approval import CLIApprovalCallback
    from ai_coding.security.user_prompt import CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService
    from ai_coding.ui.console import ConsoleObserver, DialogueConsole
    from ai_coding.ui.reader import InteractiveReader
    from ai_coding.ui.repl import run_repl

    try:
        manager = ConfigManager()
        manager.initialize(path)
        app_cfg = manager.app
        engine = build_engine_from_config(app_cfg)
        # Interactive: the model may ask a question (R6c) and a human can answer it.
        # R7c: one channel object, so `run_repl` can point it at the REPL's own
        # prompt session instead of letting a second reader fight over stdin.
        user_prompt = CLIUserPromptCallback()
        from ai_coding.ai.prompt import BASE_INSTRUCTIONS

        registry = _build_tool_registry(
            app_cfg,
            workspace,
            skills,
            user_prompt=user_prompt,
            base_instructions=BASE_INSTRUCTIONS,
        )
        # R7e-2: the queue the loop drains, and the one reader that fills it.
        steering = SteeringQueue(
            max_messages=int(getattr(app_cfg.ai, "steering_max_messages", 5)),
            max_chars=int(getattr(app_cfg.ai, "steering_max_chars", 2_000)),
        )
        reader = InteractiveReader.for_terminal(
            steering=steering, enabled=bool(getattr(app_cfg.ui, "steering", True))
        )
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"repl error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    sessions = SessionService()
    console = DialogueConsole()

    async def _serve() -> None:
        # One recorder for the whole REPL: it re-binds to whichever session is
        # current when each turn starts, and the store numbers turn files.  It is
        # built before the MCP servers connect so their failures are recorded too.
        recorder = _make_recorder(app_cfg, workspace, None)
        manager = MCPManager(app_cfg.mcp, observer=recorder)
        try:
            for tool in await manager.open_all():
                registry.register(tool)
            guards = _make_guards(app_cfg)
            hooks = _make_hooks(app_cfg, workspace)
            # Interactive REPL: confirm WARN-tier tools (write/edit/bash) with the
            # user via y/N/s prompts, rather than silently blocking them.
            loop = _build_loop(
                engine,
                sessions,
                registry,
                app_cfg,
                workspace,
                skills,
                approval=CLIApprovalCallback(),
                observer=_compose_observer(
                    ConsoleObserver(
                        console, chunks=bool(getattr(app_cfg.ui, "tool_chunks", True))
                    ),
                    recorder,
                ),
                trace=recorder,
                guards=guards,
                hooks=hooks,
                steering=steering,
                cancel=reader.cancel_requested,
            )
            try:
                await run_repl(
                    loop, sessions, console, user_prompt=user_prompt, reader=reader
                )
            except KeyboardInterrupt:
                console.info("[aborted]")
            finally:
                await loop.aclose()
        finally:
            await manager.aclose()

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
    trace: Any | None = None,
    guards: Any | None = None,
    hooks: Any | None = None,
    steering: Any | None = None,
    cancel: Any | None = None,
) -> Any:
    """Assemble the loop and its optional components (silent degradation)."""
    if approval is None:
        # The composition root owns the policy: `ask` is non-interactive and has
        # nobody to answer a WARN prompt, so it auto-approves (the DENY tier
        # still blocks dangerous calls).  `repl` passes CLIApprovalCallback.
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

    prompt = PromptAssembler(BASE_INSTRUCTIONS)

    # R3b: the on-demand recall tool needs the memory service, so the composition
    # root registers it here (the registry itself stays a pure tool container).
    if memory is not None and getattr(registry, "get", None) is not None:
        if registry.get("memory_search") is None:
            from ai_coding.tools.memory_search_tool import MemorySearchTool

            registry.register(MemorySearchTool(memory))
        # R7e-1: the model may write a note of its own, next to the retrospective
        # harvest.  Parent-only: a sub-agent stack never gets memory tools at all.
        if registry.get("memory_write") is None:
            from ai_coding.tools.memory_write_tool import MemoryWriteTool

            registry.register(MemoryWriteTool(memory))
        # R8a: the model's own line for the next turn in *this* session.  Also
        # parent-only: a sub-agent's session is temporary, so the line would be
        # written and thrown away.
        if registry.get("checkpoint_write") is None:
            from ai_coding.tools.checkpoint_tool import CheckpointTool

            registry.register(CheckpointTool())

    return AgentLoop(
        engine,
        sessions,
        registry,
        approval=approval,
        # R7d: the gate is the composition root's policy, so the servers the user
        # declared trusted are trusted here and nowhere else.
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
        # R8c: the same reader that owns the terminal owns the Esc key.
        cancel=cancel,
    )


def _make_recorder(app_cfg: Any, workspace: str, session: Any) -> Any | None:
    """Build a trace recorder for this run, or ``None`` when tracing is off.

    Tracing is a passive observer: it is composed into the braid the loop already
    talks to, so nothing about the turn changes when it is enabled
    (docs/r4-analysis.md 4.7 a9).
    """
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
    except Exception:  # noqa: BLE001 — cleanup must never block a run
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
    except Exception:  # noqa: BLE001 — pricing is optional
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
    """Build the guard chain from config, or ``None`` when nothing is enabled.

    The duplicate-call guard is on by default: repeated identical calls are a
    failure mode with no upside, and its threshold leaves one deliberate retry
    (docs/r4-analysis.md 5.2).
    """
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


async def _run_turn_with_mcp(
    *,
    engine: Any,
    app_cfg: Any,
    registry: Any,
    sessions: Any,
    session: Any,
    prompt: str,
    on_delta: Any,
    workspace: str,
    skills_path: str | None,
    approval: Any | None = None,
    observer: Any | None = None,
    trace: Any | None = None,
    guards: Any | None = None,
    hooks: Any | None = None,
) -> str:
    """Run one turn with MCP tools registered, then release everything."""
    manager = MCPManager(app_cfg.mcp, observer=trace)
    try:
        for tool in await manager.open_all():
            registry.register(tool)
        loop = _build_loop(
            engine,
            sessions,
            registry,
            app_cfg,
            workspace,
            skills_path,
            approval=approval,
            observer=observer,
            trace=trace,
            guards=guards,
            hooks=hooks,
        )
        try:
            return str(await loop.process_input(session, prompt, on_delta=on_delta))
        finally:
            await loop.aclose()
    finally:
        await manager.aclose()


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
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    *,
    user_prompt: Any | None = None,
    base_instructions: str = "",
) -> Any:
    """Build the domain tool registry the self-owned loop dispatches through.

    Permission and approval are *not* part of the registry: they belong to the
    ``ToolDispatcher`` the loop creates.

    ``user_prompt`` (R6c) is the channel ``ask_user`` would use; only an interactive
    entry point has one, so ``ask`` leaves it ``None`` and the tool is not registered.
    """
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
