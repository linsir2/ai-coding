"""Request handlers for the TUI IPC service (M0.5).

Handlers are **sync** functions; they run on the Endpoint's thread pool.
Anything that must touch the asyncio world goes through the service bridges:

* ``service.notify(...)`` — push an event (thread-safe)
* ``service.spawn(coro)`` — schedule async work (fire-and-forget)
* ``service.run(coro)`` — run async work and wait for its result

``session.prompt`` is still an **echo** in M0.5 — it proves the event
pipeline end to end; wiring the real ``AgentLoop`` is plan M2 and replaces
only this one handler's body.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ai_coding.domain.session import _message_to_dict
from ai_coding.server.service import IpcService
from ai_coding.server.world import AppWorld

_log = logging.getLogger(__name__)

__all__ = ["HandlerTable", "make_handlers"]


HandlerTable = dict[str, Callable[[dict[str, Any]], Any]]


class _WorldState:
    """Mutable bits shared by handlers (current session / turn counter)."""

    def __init__(self) -> None:
        self.session_id: str = ""
        self.turn_id: int = 0


def make_handlers(
    world: AppWorld,
    service: IpcService,
    *,
    shutdown: Callable[[], None] | None = None,
) -> HandlerTable:
    """Build the request table bound to one world + one service."""
    state = _WorldState()

    def initialize(params: dict[str, Any]) -> dict[str, Any]:
        from ai_coding import __version__

        session = world.sessions.create()
        state.session_id = session.session_id
        model = getattr(world.engine, "model_config", None)
        models = [
            {"name": str(getattr(cfg, "name", "") or key), "key": key}
            for key, cfg in (getattr(world.config, "models", None) or {}).items()
        ]
        return {
            "version": __version__,
            "workspace": world.workspace,
            "model": str(getattr(model, "name", "") or ""),
            "models": models,
            "sessionId": session.session_id,
            "sessionTitle": session.title,
        }

    def session_create(params: dict[str, Any]) -> dict[str, Any]:
        title = str(params.get("title", "") or "")
        session = world.sessions.create(title=title)
        world.sessions.save(session)  # a created session must be listable
        state.session_id = session.session_id
        return {"id": session.session_id, "title": session.title}

    def session_list(params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit", 50) or 50)
        rows = []
        for session in world.sessions.list_sessions()[:limit]:
            rows.append(
                {
                    "id": session.session_id,
                    "title": session.title,
                    "createdTime": session.created_time,
                    "lastAccessTime": session.last_access_time,
                    "preview": getattr(session, "first_preview", "") or "",
                    "count": int(getattr(session, "message_count", -1)),
                }
            )
        return {"sessions": rows}

    def session_load(params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("id", "") or state.session_id)
        session = world.sessions.load(session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        state.session_id = session.session_id
        return {
            "id": session.session_id,
            "title": session.title,
            "messages": [_message_to_dict(m) for m in session.messages],
        }

    def session_prompt(params: dict[str, Any]) -> dict[str, Any]:
        """M0.5 echo turn: same wire shape a real turn will push (M2)."""
        text = str(params.get("text", "") or "")
        session_id = str(params.get("id", "") or state.session_id)
        state.turn_id += 1
        turn_id = state.turn_id

        async def echo() -> None:
            service.notify("event", {"type": "turn.start", "data": {"turn": turn_id}})
            reply = f"echo: {text}"
            for index in range(0, len(reply), 4):
                service.notify(
                    "event", {"type": "text.delta", "data": {"text": reply[index : index + 4]}}
                )
                await asyncio.sleep(0.02)
            service.notify(
                "event",
                {
                    "type": "turn.finish",
                    "data": {"status": "completed", "text": reply, "iterations": 0},
                },
            )

        service.spawn(echo())
        return {"turnId": turn_id, "sessionId": session_id}

    def session_interrupt(params: dict[str, Any]) -> dict[str, Any]:
        world.cancel.set()
        world.cancel.clear()
        return {"ok": True}

    def session_delete(params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("id", ""))
        return {"ok": bool(session_id) and world.sessions.delete(session_id)}

    def model_set(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        world.loop.set_model(name)
        return {"ok": True, "model": name}

    def skills_list(params: dict[str, Any]) -> dict[str, Any]:
        registry = getattr(world.loop, "skills", None)
        catalog = registry.catalog_text() if registry is not None else ""
        return {"catalog": catalog}

    def config_summary(params: dict[str, Any]) -> dict[str, Any]:
        ai_cfg = getattr(world.config, "ai", None)
        return {
            "model": str(getattr(world.config, "default_model", "") or ""),
            "workspace": world.workspace,
            "maxContextTokens": int(getattr(ai_cfg, "max_context_tokens", 0) or 0),
            "maxToolIterations": int(getattr(ai_cfg, "max_tool_iterations", 0) or 0),
            "stopPolicy": str(getattr(ai_cfg, "stop_policy", "") or ""),
        }

    def shutdown_handler(params: dict[str, Any]) -> dict[str, Any]:
        if shutdown is not None:
            shutdown()
        return {"ok": True}

    return {
        "initialize": initialize,
        "session.create": session_create,
        "session.list": session_list,
        "session.load": session_load,
        "session.prompt": session_prompt,
        "session.interrupt": session_interrupt,
        "session.delete": session_delete,
        "model.set": model_set,
        "skills.list": skills_list,
        "config.summary": config_summary,
        "shutdown": shutdown_handler,
    }
