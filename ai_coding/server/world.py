"""The application world the IPC server serves (M0).

The single composition root for the backend: config -> engine -> tools ->
MCP -> AgentLoop.  The CLI launches it through ``server/launcher.py`` for
the OpenTUI frontend; the retired Textual UI assembled the same pieces
itself (its module tree under ``ai_coding/ui/`` is gone).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

__all__ = ["AppWorld", "build_world"]


@dataclass
class AppWorld:
    """Everything one TUI run owns; closed together in ``aclose``."""

    config: Any
    engine: Any
    registry: Any
    sessions: Any
    loop: Any
    mcp: Any
    workspace: str
    skills: str | None
    cancel: asyncio.Event
    steering: Any
    #: The WARN-tier approval callback; the IPC layer installs its provider
    # (handlers.py::_wire_approval) so the panel can answer it (M5).
    approval: Any
    #: The model's question channel; the IPC layer installs its provider
    # (handlers.py::_wire_user_prompt) so the panel can answer it.  Without a
    # provider it falls back to the stdin prompt (the non-TUI entry points).
    user_prompt: Any

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


async def build_world(path: str | None, workspace: str, skills: str | None) -> AppWorld:
    """Build config -> engine -> registry -> MCP -> loop (never starts a UI)."""
    from ai_coding.cli import _build_loop, _build_tool_registry, _make_hooks, _make_recorder
    from ai_coding.config.loader import ConfigManager
    from ai_coding.core.guard import build_guards
    from ai_coding.core.mcp import MCPManager
    from ai_coding.core.steering import SteeringQueue
    from ai_coding.security.approval import CLIApprovalCallback
    from ai_coding.security.user_prompt import CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService

    manager = ConfigManager()
    manager.initialize(path)
    app_cfg = manager.app

    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.ai.prompt import BASE_INSTRUCTIONS

    engine = build_engine_from_config(app_cfg)
    # The question channel: with the TUI's provider installed by
    # handlers._wire_user_prompt the model's questions reach the panel; the
    # stdin fallback serves the non-TUI entry points.
    user_prompt = CLIUserPromptCallback()
    registry = _build_tool_registry(
        app_cfg, workspace, skills,
        user_prompt=user_prompt, base_instructions=BASE_INSTRUCTIONS,
    )

    steering = SteeringQueue()
    sessions = SessionService()
    recorder = _make_recorder(app_cfg, workspace, None)
    mcp_manager = MCPManager(app_cfg.mcp, observer=recorder)
    for tool in await mcp_manager.open_all():
        registry.register(tool)

    # M5: approvals go to the TUI as a server-initiated request; the IPC layer
    # installs the provider in ``make_handlers``.  Without one (a stray
    # non-TUI use) the callback falls back to the stdin prompt.
    approval = CLIApprovalCallback()
    cancel = asyncio.Event()
    loop = _build_loop(
        engine, sessions, registry, app_cfg, workspace, skills,
        approval=approval,
        observer=None,
        trace=recorder,
        guards=build_guards(app_cfg),
        hooks=_make_hooks(app_cfg, workspace),
        steering=steering,
        cancel=cancel,
    )
    return AppWorld(
        config=app_cfg,
        engine=engine,
        registry=registry,
        sessions=sessions,
        loop=loop,
        mcp=mcp_manager,
        workspace=workspace,
        skills=skills,
        cancel=cancel,
        steering=steering,
        approval=approval,
        user_prompt=user_prompt,
    )
