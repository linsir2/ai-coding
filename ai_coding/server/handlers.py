"""Request handlers for the TUI IPC service (M0.5/M2).

Handlers are **sync** functions; they run on the Endpoint's thread pool.
Anything that must touch the asyncio world goes through the service bridges:

* ``service.notify(...)`` — push an event (thread-safe)
* ``service.spawn(coro)`` — schedule async work and keep its future
* ``service.run(coro)`` — run async work and wait for its result

M2: ``session.prompt`` runs the real ``AgentLoop.process_input`` with an
:class:`~ai_coding.server.bridge.EventBridge` on both channels (deltas and
observer); ``session.interrupt`` sets the tool-batch cancel event *and*
cancels the running turn future — the two layers the loop itself documents
(core/agent_loop.py:570, :932).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from ai_coding.domain.session import _message_to_dict
from ai_coding.server.bridge import EventBridge
from ai_coding.server.display import provider_from_base_url
from ai_coding.server.service import IpcService
from ai_coding.server.vcs import git_branch
from ai_coding.server.world import AppWorld

_log = logging.getLogger(__name__)

__all__ = ["HandlerTable", "make_handlers"]


HandlerTable = dict[str, Callable[[dict[str, Any]], Any]]


class _WorldState:
    """Mutable bits shared by handlers (live session, turn counter, futures)."""

    def __init__(self) -> None:
        self.session_id: str = ""
        #: The live ``SessionData`` the turns run against; initialize/
        #: session.create/session.load replace it.
        self.session: Any = None
        self.turn_id: int = 0
        #: Not-done turn futures; interrupt cancels them.  Done futures are
        #: pruned by their own callback.
        self.turn_futures: set[Future[Any]] = set()


def make_handlers(
    world: AppWorld,
    service: IpcService,
    *,
    shutdown: Callable[[], None] | None = None,
) -> HandlerTable:
    """Build the request table bound to one world + one service."""
    state = _WorldState()
    bridge = EventBridge(lambda method, params: service.notify(method, params))

    def initialize(params: dict[str, Any]) -> dict[str, Any]:
        from ai_coding import __version__

        session = world.sessions.create()
        state.session = session
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
            "modelKey": str(getattr(world.config, "default_model", "") or ""),
            "provider": provider_from_base_url(str(getattr(model, "base_url", "") or "")),
            "vcs": {"branch": git_branch(world.workspace)},
            "models": models,
            "sessionId": session.session_id,
            "sessionTitle": session.title,
        }

    def session_create(params: dict[str, Any]) -> dict[str, Any]:
        title = str(params.get("title", "") or "")
        session = world.sessions.create(title=title)
        world.sessions.save(session)  # a created session must be listable
        state.session = session
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
        state.session = session
        state.session_id = session.session_id
        return {
            "id": session.session_id,
            "title": session.title,
            "messages": [_message_to_dict(m) for m in session.messages],
        }

    def session_prompt(params: dict[str, Any]) -> dict[str, Any]:
        """Start a real turn; events reach the client through the bridge."""
        text = str(params.get("text", "") or "")
        session_id = str(params.get("id", "") or state.session_id)
        session = state.session
        if session is None:
            raise ValueError("no session initialized")
        if session_id != state.session_id:
            # Loading other sessions is the sessions dialog's job (M3); running
            # the *current* session under a foreign id would be silent data loss.
            raise ValueError(f"session not loaded: {session_id}")
        state.turn_id += 1
        turn_id = state.turn_id

        async def run_turn() -> None:
            try:
                await world.loop.process_input(
                    session, text, on_delta=bridge.on_delta, observer=bridge
                )
            except asyncio.CancelledError:
                # The loop already persisted the partial turn and emitted
                # TurnFinished(ABORTED) before re-raising (core/agent_loop.py:570);
                # this coroutine is the task's top, so there is nothing above
                # to observe the cancellation.
                pass
            except Exception as exc:  # noqa: BLE001 — a bug must not hang the UI
                _log.exception("turn runner failed outside the loop")
                service.notify(
                    "event",
                    {
                        "type": "turn.finished",
                        "data": {
                            "status": "error",
                            "reason": f"{type(exc).__name__}: {exc}"[:300],
                            "iterations": 0,
                            "truncatedByLength": False,
                            "handoff": "",
                        },
                    },
                )

        future = service.spawn(run_turn())
        state.turn_futures.add(future)
        future.add_done_callback(state.turn_futures.discard)
        return {"turnId": turn_id, "sessionId": session_id}

    def session_interrupt(params: dict[str, Any]) -> dict[str, Any]:
        # Two layers, both needed (ui/textual_app.py::action_cancel_turn):
        # the event is the tool batch's fast path (the loop clears it before
        # each batch), the future cancellation reaches a model call.
        world.cancel.set()
        interrupted = sum(1 for future in list(state.turn_futures) if future.cancel())
        return {"ok": True, "interrupted": interrupted}

    def session_delete(params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("id", ""))
        return {"ok": bool(session_id) and world.sessions.delete(session_id)}

    def model_set(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        world.loop.set_model(name)
        # The metadata label reads the endpoint of the *active* model, so a
        # switch across providers must report the new one.
        model = getattr(world.engine, "model_config", None)
        return {
            "ok": True,
            "model": name,
            "provider": provider_from_base_url(str(getattr(model, "base_url", "") or "")),
        }

    def skills_list(params: dict[str, Any]) -> dict[str, Any]:
        """Structured rows for the skills dialog (M3).

        The model's catalogue stays on ``registry.catalog_text()`` (system
        prompt); this is only what the human list needs.
        """
        registry = getattr(world.loop, "skills", None)
        if registry is None:
            return {"skills": []}
        rows = []
        for name in registry.names():
            meta = registry.meta(name)
            rows.append(
                {
                    "name": name,
                    "description": meta.get("description"),
                    "phase": meta.get("phase") or "",
                }
            )
        return {"skills": rows}

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
