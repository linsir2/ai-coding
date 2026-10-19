"""The application world the IPC server serves (M0).

The single composition root for the backend: config -> engine -> tools ->
MCP -> AgentLoop.  The CLI launches it through ``server/launcher.py`` for
the OpenTUI frontend; the retired Textual UI assembled the same pieces
itself (its module tree under ``ai_coding/ui/`` is gone).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

__all__ = ["AppWorld", "build_world"]


@dataclass
class AppWorld:
    """Everything one TUI run owns; closed together in ``aclose``."""

    config: Any
    #: Where the config was loaded from, so a runtime-added MCP server has
    #: somewhere to persist beside (M5, ``mcp.local.yaml``).
    config_path: str | None
    engine: Any
    registry: Any
    sessions: Any
    loop: Any
    mcp: Any
    workspace: str
    skills: str | None
    cancel: asyncio.Event
    notices: Any
    #: D-1b: the workspace's background jobs.  The IPC layer answers
    #: ``session.jobs`` from it, and the panel renders what it returns.
    jobs: Any
    #: The WARN-tier approval callback; the IPC layer installs its provider
    # (handlers.py::_install_providers) so the panel can answer it (M5), and
    approval: Any
    #: is re-armed after every command that may swap it (I1).  The
    #: model's question channel is installed the same way.
    user_prompt: Any
    #: 11-spec §7.2: the newest armed session, found at boot; ``initialize``
    #: consumes it exactly once (loading it as live and resuming its driver).
    pending_resume: Any = None

    async def aclose(self) -> None:
        for closer in (
            getattr(self.loop, "aclose", None),
            getattr(self.mcp, "aclose", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                _log.debug("world close failed", exc_info=True)


def find_armed_session(sessions: Any) -> Any | None:
    """The newest persisted session whose task is armed (11-spec §7.2).

    Scanned from the raw JSON keys, not ``list_sessions`` -- this runs before
    any session UI, and the message bodies are not needed to pick a winner.
    ``openTurn`` is deliberately NOT part of the filter: a clean close clears
    it, and a crash between rounds leaves exactly that state.
    """
    root = Path(getattr(sessions, "root", "") or "")
    if not root.is_dir():
        return None
    best: tuple[str, str] | None = None
    for path in root.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task = raw.get("task")
        if not isinstance(task, dict) or task.get("armed") is not True:
            continue
        # R07: only a profile a runner can actually drive competes for the
        # resume.  A stale armed task with an unknown profile used to win the
        # scan (it was the newest) and then be dropped in initialize, which
        # swallowed the one recovery chance a newer valid session still had.
        if task.get("armedProfile") not in ("plan", "goal"):
            continue
        stamp = str(raw.get("lastAccessTime", "") or "")
        if best is None or stamp > best[0]:
            best = (stamp, path.stem)
    if best is None:
        return None
    return sessions.load(best[1])


async def build_world(path: str | None, workspace: str, skills: str | None) -> AppWorld:
    """Build config -> engine -> registry -> MCP -> loop (never starts a UI)."""
    from ai_coding.cli import _build_loop, _build_tool_registry, _make_hooks, _make_recorder
    from ai_coding.config.loader import ConfigLoader
    from ai_coding.core.guard import build_guards
    from ai_coding.core.jobs import JobRegistry
    from ai_coding.core.mcp import MCPManager, build_mcp_catalog
    from ai_coding.core.steering import SteeringQueue
    from ai_coding.paths import Paths
    from ai_coding.security.approval import CLIApprovalCallback
    from ai_coding.security.user_prompt import CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService
    from ai_coding.skills.registry import SkillRegistry
    from ai_coding.tools.mcp_call import MCPCallTool

    paths = Paths.for_workspace(workspace)
    # Layered: the user-global config as the base, the project's on top (M9).
    # With no project file, the global one still answers — which is what makes
    # `ai-coding` startable from a fresh directory with only a personal key.
    app_cfg = ConfigLoader.load_layers(paths, path)

    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.ai.prompt import BASE_INSTRUCTIONS

    engine = build_engine_from_config(app_cfg)
    # The question channel: with the TUI's provider installed by
    # handlers._install_providers the model's questions reach the panel; the
    # stdin fallback serves the non-TUI entry points.
    user_prompt = CLIUserPromptCallback()
    # One root, resolved once, and the *same* registry object handed to both the
    # catalog (which the system prompt reads) and the tool (which loads one).
    # They used to be given different things: the loop always got
    # `<workspace>/skills` while the tool was registered only when `--skills` was
    # passed, so `ai-coding -w .` advertised skills with no way to load them.
    # Both scopes, project last so it wins. `--skills` still overrides, and now
    # it overrides *everything* rather than only the project half.
    roots = (
        (Path(skills).expanduser().resolve(),) if skills else paths.skill_roots
    )
    skill_registry = SkillRegistry(roots)
    for name, scope in skill_registry.shadowed():
        _log.warning("skill %r at %s is shadowed (project scope wins)", name, scope)
    # P0-2: the harness lane, built before the registry because the sub-agent
    # runner is constructed *inside* it -- a queue nobody holds is a queue
    # nobody writes to, which is how a background completion used to reach the
    # operator and nobody else.
    notices = SteeringQueue()
    # D-1: one job table per workspace, beside the sessions and traces rather
    # than inside either -- a background command outlives the session that
    # started it, and the ledger that says so has to outlive it too.
    jobs = JobRegistry(root=Path(workspace) / ".ai-coding" / "jobs", notices=notices)
    registry = _build_tool_registry(
        app_cfg, workspace, skill_registry,
        user_prompt=user_prompt, base_instructions=BASE_INSTRUCTIONS,
        notices=notices,
        jobs=jobs,
    )

    # Per-workspace state lives under the workspace, beside the traces
    # (cli.py::_make_recorder resolves the same ``.ai-coding/<kind>`` shape).
    # The service's default is relative, which resolved against whatever
    # directory the process started in — so a user's conversations landed in
    # the source tree they happened to launch from (A7).
    sessions = SessionService(
        paths.sessions
    )
    recorder = _make_recorder(app_cfg, str(paths.workspace), None)
    mcp_manager = MCPManager(app_cfg.mcp, observer=recorder)
    await mcp_manager.open_all()
    # M2: one dispatcher carries every remote call, and the catalog it is
    # chosen from is built from the same manager — the text the model reads and
    # the calls it can make come from one source, so they cannot drift apart.
    #
    # Registered only when MCP is switched on (M6).  With it off the catalog
    # builder returns "" forever, so the tool's description would point at a
    # block that never arrives and every call would come back as an error —
    # 556 characters of schema per step spent advertising a dead end.  The gate
    # still knows ``mcp_call`` by name; that is for a hallucinated pair while
    # MCP *is* on, and is unaffected.
    if app_cfg.mcp.enabled:
        registry.register(MCPCallTool(mcp_manager))

    # M5: approvals go to the TUI as a server-initiated request; the IPC layer
    # installs the provider in ``make_handlers``.  Without one (a stray
    # non-TUI use) the callback falls back to the stdin prompt.
    approval = CLIApprovalCallback()
    cancel = asyncio.Event()
    loop = _build_loop(
        engine, sessions, registry, app_cfg, str(paths.workspace), skill_registry,
        catalog_builder=lambda: build_mcp_catalog(mcp_manager.available()),
        approval=approval,
        observer=None,
        trace=recorder,
        guards=build_guards(app_cfg),
        hooks=_make_hooks(app_cfg, str(paths.workspace)),
        notices=notices,
        jobs=jobs,
        cancel=cancel,
    )
    pending = find_armed_session(sessions)
    if pending is not None:
        _log.info("boot resume candidate: %s", pending.session_id)
    return AppWorld(
        config=app_cfg,
        config_path=str(path) if path else None,
        engine=engine,
        registry=registry,
        sessions=sessions,
        loop=loop,
        jobs=jobs,
        mcp=mcp_manager,
        workspace=str(paths.workspace),
        skills=skills,
        cancel=cancel,
        notices=notices,
        approval=approval,
        user_prompt=user_prompt,
        pending_resume=pending,
    )
