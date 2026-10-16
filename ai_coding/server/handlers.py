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
import re
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

from ai_coding.attachments import save_data_url
from ai_coding.config.models import resolve_context_window
from ai_coding.domain.session import _message_to_dict
from ai_coding.infra.vcs import git_branch
from ai_coding.server.bridge import EventBridge, Notify
from ai_coding.server.display import provider_from_base_url
from ai_coding.server.service import IpcService
from ai_coding.server.world import AppWorld

_log = logging.getLogger(__name__)

__all__ = ["HandlerTable", "make_handlers"]


HandlerTable = dict[str, Callable[[dict[str, Any]], Any]]


def _install_providers(world: AppWorld, service: IpcService) -> None:
    """Route both question channels to the client as JSON-RPC requests.

    Called twice on purpose, and idempotent: once when the handler table is
    built, and again after every ``session.command`` run.  A command can swap
    the callbacks out from under us — ``/permissions confirm`` installs a
    FRESH ``CLIApprovalCallback`` with no provider — and a callback without a
    provider falls back to reading **stdin**, which in a full-screen TUI means
    the turn waits on the terminal and the approval panel never appears.  The
    retired Textual UI re-armed this after every slash command for exactly that
    reason (``ui/textual_app.py::_ensure_approval_provider``).

    Approvals answer with the CLI vocabulary (``yes``/``always``/``no``/
    ``stop``) so one parser serves both front ends (security/approval.py:88-98)
    and a failed round-trip stays fail-closed inside ``approve`` itself.
    Questions answer with the plain text the ``ask_user`` tool renders; an
    empty answer (esc, a closed panel) becomes ``reason="empty"`` inside
    ``CLIUserPromptCallback`` so the model decides for itself.
    """
    dispatcher = getattr(getattr(world, "loop", None), "dispatcher", None)
    approval = getattr(dispatcher, "approval", None) or getattr(world, "approval", None)
    set_approval_provider = getattr(approval, "set_provider", None)
    if set_approval_provider is not None:

        async def approval_provider(tool_name: str, params: dict[str, Any], reason: str) -> str:
            return str(
                await service.call(
                    "approval.request",
                    {"toolName": tool_name, "params": params, "reason": reason},
                )
            )

        set_approval_provider(approval_provider)

    prompt = getattr(world, "user_prompt", None)
    set_prompt_provider = getattr(prompt, "set_provider", None)
    if set_prompt_provider is not None:

        async def question_provider(
            question: str, options: list[str], detail: str = "", intent: str = ""
        ) -> str:
            raw = await service.call(
                "user_prompt.ask",
                {
                    "question": question,
                    "options": list(options),
                    # 19 §3.2: always explicit; plain readers ignore the keys.
                    "detail": detail,
                    "intent": intent,
                },
            )
            return str(raw or "")

        set_prompt_provider(question_provider)


class IpcConsole:
    """The console ``handle_input`` writes through, over IPC (A1/M6.5).

    ``delta`` rides the bridge — the same ``text.delta`` event a live turn
    sends.  ``info``/``error``/``rule`` become ``console.line`` events the
    frontend renders as dim lines: the port of the old Textual UI's
    ``Block("info")``.  There is deliberately no ``ask``: the command surface
    never asks — a bare ``/plan`` enters read-only plan mode and the topic
    rides the command line (``/plan <topic>``), so the stdin prompt the REPL
    uses has no IPC counterpart to build.
    """

    def __init__(self, notify: Notify, on_delta: Callable[[str], None]) -> None:
        self._notify = notify
        self._on_delta = on_delta

    def delta(self, token: str) -> None:
        self._on_delta(token)

    def step(self, title: str) -> Any:
        """No folding over IPC; the caller prints what it needs."""
        return None

    def info(self, message: str) -> None:
        self._line("info", message)

    def error(self, message: str) -> None:
        self._line("error", message)

    def rule(self, title: str = "") -> None:
        if title:
            self._line("info", title)

    def _line(self, tone: str, text: str) -> None:
        if not text:
            return
        self._notify("event", {"type": "console.line", "data": {"tone": tone, "text": text}})


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


#: 11-spec §7.2: boot auto-resumes are capped; past this the task is disarmed
#: with a notice instead of flapping forever (OpenCode's countResume shape).
_MAX_BOOT_RESUMES = 5


def _persist_session(world: Any, session: Any) -> None:
    """Best-effort durable save (11-spec §7): the loop's closer when it has
    one (tool pairing + degraded reporting), plain service save otherwise."""
    save = getattr(getattr(world.loop, "closer", None), "save", None)
    if save is None:
        save = getattr(world.sessions, "save", None)
    if save is not None:
        save(session)


def context_tokens(session: Any) -> int:
    """The newest turn's prompt tokens that still describe the live context.

    Measured live: after ``/compact`` the sidebar still read 237,157 / 100%, because
    it reported the last turn's prompt tokens and a compaction produces no turn.
    The pre-compaction number describes a context that no longer exists, so
    presenting it as the current occupancy is a false statement about how full the
    window is.

    So usage from a turn at or before the boundary is discarded, and the meter
    reads zero until a turn has actually been measured against the compacted
    history. OpenCode does the same by dropping every assistant usage recorded
    before the latest completed compaction.
    """
    if session is None:
        return 0
    # The same question the compaction gate asks, answered by the same code:
    # two readers of one rule is what let the gate and the meter disagree once.
    return int(session.live_prompt_tokens())


def make_handlers(
    world: AppWorld,
    service: IpcService,
    *,
    shutdown: Callable[[], None] | None = None,
) -> HandlerTable:
    """Build the request table bound to one world + one service."""
    state = _WorldState()
    bridge = EventBridge(lambda method, params: service.notify(method, params))
    _install_providers(world, service)
    # M3: MCP state changes are wire events too, so the dialog does not have to
    # poll a manager that reconnects on its own.  Attached here because this is
    # where the service exists; events published before it can have no listener,
    # and the dialog's fetch-on-open covers the initial state.
    mcp_manager = getattr(world, "mcp", None)
    if mcp_manager is not None and hasattr(mcp_manager, "attach_status_notifier"):
        # The wire speaks one notification method, "event", whose payload names
        # the type (frontend/src/ipc.ts subscribes to *that*).  The manager says
        # "this server settled" without knowing the envelope, so the translation
        # happens here, where every other event's does.
        mcp_manager.attach_status_notifier(
            lambda event_type, data: service.notify(
                "event", {"type": event_type, "data": data}
            )
        )

    def initialize(params: dict[str, Any]) -> dict[str, Any]:
        from ai_coding import __version__

        # 11-spec §7.2: consume the boot candidate exactly once.  An armed
        # task is the user's standing consent to keep working, so it is
        # loaded live and its driver resumes -- unless the breaker says stop.
        # (Audit fix: every mutation below goes through the RELOADED session;
        # the pre-load object is a different instance and saving it would
        # leave the disk copy armed.)
        pending = getattr(world, "pending_resume", None)
        world.pending_resume = None
        boot_session = None
        if pending is not None:
            candidate = world.sessions.load(pending.session_id) or pending
            candidate_task = getattr(candidate, "task", None)
            if (
                candidate_task is not None
                and getattr(candidate_task, "armed", False)
                and getattr(candidate_task, "armed_profile", "") == "plan"
            ):
                boot_session = candidate
        if boot_session is not None:
            session = boot_session
            state.session = session
            state.session_id = session.session_id
            task = session.task
            if int(getattr(task, "resume_count", 0) or 0) >= _MAX_BOOT_RESUMES:
                task.armed = False
                _persist_session(world, session)
                service.notify(
                    "event",
                    {
                        "type": "console.line",
                        "data": {
                            "tone": "error",
                            "text": (
                                f"[plan] 自动续跑已达上限（{_MAX_BOOT_RESUMES} 次），"
                                "任务已暂停；检查后继续对话或重新进入 Plan 模式。"
                            ),
                        },
                    },
                )
            else:
                _launch_boot_resume(session)
        else:
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
        # P5: ``load`` already derived the two recovery facts; until now they
        # were read and thrown away.  They ride this response rather than a new
        # event, the same way ``rejected`` rides ``session.prompt``'s.
        return {
            "id": session.session_id,
            "title": session.title,
            "messages": [_message_to_dict(m) for m in session.messages],
            "recoveredInterrupted": bool(session.recovered_interrupted),
            "openTurn": int(session.open_turn or 0),
            "pendingApproval": dict(session.pending_approval or {}),
            # P5 (18 §10.5): same two posture keys as session.stats, always.
            "mode": str(getattr(session, "mode", "normal") or "normal"),
            "readonly": bool(getattr(session, "readonly", False)),
        }

    def _launch_turn(run: Callable[[], Coroutine[Any, Any, None]]) -> int:
        """Spawn a turn coroutine and track it (08).

        Shared by ``session.prompt`` and ``session.resume``: the guarded shape
        -- cancellation passes through, an outside failure becomes the
        ``turn.finished`` error event -- is the contract both callers need, and
        writing it twice is how the two drift apart.
        """
        async def guarded() -> None:
            try:
                await run()
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

        state.turn_id += 1
        future = service.spawn(guarded())
        state.turn_futures.add(future)
        future.add_done_callback(state.turn_futures.discard)
        return state.turn_id

    def _launch_boot_resume(session: Any) -> None:
        """Resume an armed task after a restart (11-spec §7.2): the interrupted
        round first (when one is open), then the plan run itself.

        The run goes through ``PlanRunner.run``, not the driver directly --
        that entry carries the whole armed lifecycle (arm / roundsRun /
        disarm persistence), which a bare driver call would bypass.
        """

        async def run_resume() -> None:
            from ai_coding.core.plan_runner import PlanRunner

            task = getattr(session, "task", None)
            # Count BEFORE resuming: if this resume crashes too, the count is
            # already on disk and the breaker keeps converging.
            if task is not None:
                task.resume_count = int(getattr(task, "resume_count", 0) or 0) + 1
                _persist_session(world, session)
            if session.open_turn is not None:
                await world.loop.resume_turn(
                    session, on_delta=bridge.on_delta, observer=bridge
                )
            if not getattr(task, "armed", False):
                return
            console = IpcConsole(
                lambda method, message: service.notify(method, message), bridge.on_delta
            )
            await PlanRunner(world.loop).run(
                session, console, on_delta=bridge.on_delta, observer=bridge
            )

        _launch_turn(run_resume)

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
        # r17/T5: clipboard images arrive as data URLs; they are written under
        # the workspace once and the message references the file.
        images: list[Any] = []
        rejected: list[dict[str, str]] = []
        for item in params.get("images", []) or []:
            if not isinstance(item, dict):
                continue
            saved = save_data_url(
                world.workspace,
                str(item.get("name", "") or ""),
                str(item.get("data_url", "") or ""),
            )
            # r20/F5: the transcript has already drawn this picture by the time
            # the request lands here, so dropping it in silence left the user
            # looking at an image the model never received.  Name it and say
            # which rule it broke.
            if saved.ref is not None:
                images.append(saved.ref)
            else:
                rejected.append(
                    {"name": str(item.get("name", "") or "图片"), "reason": saved.reason}
                )
        async def run_turn() -> None:
            await world.loop.process_input(
                session,
                text,
                on_delta=bridge.on_delta,
                observer=bridge,
                # offered only when there is something to offer, so loop
                # doubles that predate r17/T5 stay callable for text turns
                **({"images": tuple(images)} if images else {}),
            )

        turn_id = _launch_turn(run_turn)
        # ``rejected`` rides the response the client already awaits, so no new
        # IPC call and no new event type is needed to tell the user a paste did
        # not make it.
        return {"turnId": turn_id, "sessionId": session_id, "rejected": rejected}

    def session_resume(params: dict[str, Any]) -> dict[str, Any]:
        """Continue the interrupted turn (08); events reach the client through
        the bridge, same as a prompted turn."""
        session_id = str(params.get("id", "") or state.session_id)
        session = state.session
        if session is None:
            raise ValueError("no session initialized")
        if session_id != state.session_id:
            # Same rule as session_prompt: running the *current* session under
            # a foreign id would be silent data loss.
            raise ValueError(f"session not loaded: {session_id}")

        async def run_resumed() -> None:
            await world.loop.resume_turn(
                session,
                on_delta=bridge.on_delta,
                observer=bridge,
            )

        turn_id = _launch_turn(run_resumed)
        return {"turnId": turn_id, "sessionId": session_id}

    def session_interrupt(params: dict[str, Any]) -> dict[str, Any]:
        # 11-spec §8: disarm BEFORE cancelling — the driver loop re-checks
        # task.armed between rounds and must not open the next one after this
        # round unwinds.  Best-effort: the round's own close save carries the
        # same in-memory object, so both writes agree.
        session = getattr(state, "session", None)
        task = getattr(session, "task", None) if session is not None else None
        if task is not None and getattr(task, "armed", False):
            task.armed = False
            _persist_session(world, session)
        # Two layers, both needed (ported from ui/textual_app.py::
        # action_cancel_turn): the event is the tool batch's fast path (the
        # loop clears it before each batch), the future cancellation reaches
        # a model call.
        world.cancel.set()
        interrupted = sum(1 for future in list(state.turn_futures) if future.cancel())
        return {"ok": True, "interrupted": interrupted}

    def session_command(params: dict[str, Any]) -> dict[str, Any]:
        """Run a slash command through the REPL dispatcher (A1).

        One entry for every command — the dispatch itself is
        ``app/commands.py::handle_input``, the backend's command design, so
        the TUI gains the whole command layer (``/plan``, ``/review``,
        ``/compact``, …) without a second implementation.  A command may run
        several turns (``/plan`` runs one per step); each of the loop's
        ``TurnFinished`` events is a *step* (``turn.step``), and the run's own
        terminal event is emitted after ``handle_input`` returns — the same
        one-command-one-turn-end shape the old TUI had.
        """
        from ai_coding.app.commands import REPLAction, handle_input

        text = str(params.get("text", "") or "")
        session_id = str(params.get("id", "") or state.session_id)
        session = state.session
        if session is None:
            raise ValueError("no session initialized")
        if session_id != state.session_id:
            raise ValueError(f"session not loaded: {session_id}")
        state.turn_id += 1
        turn_id = state.turn_id
        console = IpcConsole(
            lambda method, message: service.notify(method, message), bridge.on_delta
        )

        async def run_command() -> None:
            action_name = "continue"
            reload_in_place = False
            status, reason = "completed", ""
            # The transcript is cumulative (OpenCode parity): live rows stay
            # where they landed, so a command that rewrote the history no
            # longer asks the client to re-project and wipe them.  A reload
            # is only needed when the live session itself switched
            # (/clear, /resume — detected by identity, not by sniffing the
            # command text).  A same-session rewrite (/compact today) rides
            # as an explicit `compacted` fact for the toast instead.
            history_before = len(getattr(session, "messages", ()) or ())
            bridge.command_run = True
            try:
                action, next_session = await handle_input(
                    world.loop, text, session, world.sessions, console, observer=bridge
                )
                if isinstance(action, REPLAction):
                    action_name = action.value
                # /undo and /redo rewrite this session's history in place: the
                # client must re-project it (r17/T1).
                reload_in_place = action is REPLAction.RELOAD
                if action is REPLAction.NEW_SESSION and next_session is not None:
                    # /clear and /resume <id>: the live session follows the
                    # dispatcher, and it must be listable/loadable so the
                    # tab strip can switch to it.
                    world.sessions.save(next_session)
                    state.session = next_session
                    state.session_id = str(
                        getattr(next_session, "session_id", "") or state.session_id
                    )
            except asyncio.CancelledError:
                # The loop's own TurnFinished(ABORTED) already went out as a
                # step; the run itself ends aborted.
                status, reason = "aborted", "interrupted"
            except Exception as exc:  # noqa: BLE001 — a bug must not hang the UI
                _log.exception("command runner failed outside the dispatcher")
                status, reason = "error", f"{type(exc).__name__}: {exc}"[:300]
            finally:
                bridge.command_run = False
                # A command may have swapped the callbacks (/permissions
                # confirm installs a fresh, unprovided one) — re-arm before the
                # next turn can ask anything (I1/I2).
                _install_providers(world, service)
            service.notify(
                "event",
                {
                    "type": "turn.finished",
                    "data": {
                        "status": status,
                        "reason": reason,
                        "iterations": 0,
                        "truncatedByLength": False,
                        "handoff": "",
                        "action": action_name,
                        # the live session after the command (a NEW_SESSION
                        # action has switched it)
                        "sessionId": state.session_id,
                        # A switched session must re-project; a same-session
                        # rewrite (/compact) only reports the fact — except
                        # /undo and /redo, which *rewrite the history in place*
                        # and therefore do need the client to re-project
                        # (r17/T1).  The text rides along so the composer can
                        # get the undone user message back.
                        "reload": state.session is not session or reload_in_place,
                        "undo": (
                            world.loop.last_undo_for(state.session_id)
                            if reload_in_place
                            else None
                        ),
                        "messagesBefore": history_before,
                        "messagesAfter": len(getattr(session, "messages", ()) or ()),
                        "compacted": (
                            {
                                "before": history_before,
                                "after": len(getattr(session, "messages", ()) or ()),
                            }
                            if state.session is session
                            and len(getattr(session, "messages", ()) or ())
                            < history_before
                            else None
                        ),
                    },
                },
            )
            if action_name == "quit" and shutdown is not None:
                shutdown()

        future = service.spawn(run_command())
        state.turn_futures.add(future)
        future.add_done_callback(state.turn_futures.discard)
        return {"turnId": turn_id, "sessionId": session_id}

    def session_delete(params: dict[str, Any]) -> dict[str, Any]:
        """Delete a session; deleting the live one moves the live session (J6).

        Without the switch the next turn would persist into the deleted file
        and recreate it — a delete that does not stick.  The new session id
        rides the answer so the client can follow the tab strip to it (the
        same contract ``/clear`` uses via the terminal event's sessionId).
        """
        session_id = str(params.get("id", ""))
        if not session_id:
            return {"ok": False, "sessionId": ""}
        deleted = world.sessions.delete(session_id)
        was_live = session_id == state.session_id
        if not (deleted or was_live):
            # Nothing went away and nothing was live under that id: the client
            # is holding a stale id, and saying "failed" tells it to refresh.
            return {"ok": False, "sessionId": ""}
        # A live session that has not run a turn yet has no file, so `delete`
        # cannot unlink it — but it is gone the moment we move the live session
        # off it, which is why `was_live` alone is enough to call this a delete.
        replacement = ""
        if was_live:
            fresh = world.sessions.create()
            world.sessions.save(fresh)
            state.session = fresh
            state.session_id = fresh.session_id
            replacement = fresh.session_id
        return {"ok": deleted or was_live, "sessionId": replacement}

    def permissions_state(params: dict[str, Any]) -> dict[str, Any]:
        """Read-only view for the permissions dialog (A3.1).

        Current policy is the approval callback's own kind — there is no policy
        field anywhere (J1): ``AutoApproveCallback`` means auto, anything else
        means confirm.  Grants and their on-disk home come from the gate, which
        is also what ``/permissions clear`` empties.
        """
        from ai_coding.security.approval import AutoApproveCallback

        dispatcher = getattr(world.loop, "dispatcher", None)
        approval = getattr(dispatcher, "approval", None)
        gate = getattr(dispatcher, "gate", None)
        store = getattr(gate, "_store", None)
        return {
            "policy": "auto" if isinstance(approval, AutoApproveCallback) else "confirm",
            "grants": list(getattr(gate, "grants", ()) or ()),
            "storePath": str(getattr(store, "path", "") or ""),
        }

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

    def slash_list(params: dict[str, Any]) -> dict[str, Any]:
        """The dispatcher's vocabulary, so the client never guesses (r19/F5)."""
        from ai_coding.app.slash import command_names

        return {"ok": True, "commands": list(command_names())}

    def files_find(params: dict[str, Any]) -> dict[str, Any]:
        """Workspace files/directories for the composer's @ suggestions (r17/T4).

        A bounded walk with the same ignore list the grep fallback uses, so the
        list the user picks from and the tree the tools see agree.  Directories
        end with '/'; the client fuzz-sorts (it already ships fuzzysort).
        """

        query = str(params.get("query", "") or "").strip().casefold()
        try:
            limit = int(params.get("limit", 200) or 200)
        except (TypeError, ValueError):
            limit = 200
        from ai_coding.core.workspace_files import walk_workspace

        found: list[str] = []
        for rel, is_dir in walk_workspace(world.workspace):
            name = rel.as_posix() + "/" if is_dir else rel.as_posix()
            if query and query not in name.casefold():
                continue
            found.append(name)
            if len(found) >= limit:
                break
        return {"files": found}

    def skills_list(params: dict[str, Any]) -> dict[str, Any]:
        """Structured rows for the skills dialog (M3).

        The model's catalogue is ``skills.catalog.build_catalog`` in the system
        prompt; this is only what the human list needs. Both read the same
        parsed objects, so the two can no longer disagree about a skill.
        """
        registry = getattr(world.loop, "skills", None)
        if registry is None:
            return {"skills": []}
        return {
            "skills": [
                {
                    "name": skill.id,
                    "description": skill.description or None,
                    "phase": skill.phase or "",
                }
                for skill in registry.all()
            ]
        }

    def _memory_facade() -> Any:
        """The loop's memory facade, or ``None`` where there is no memory.

        Read through the loop rather than from a world attribute: the store is
        built inside ``_build_loop`` and never handed to ``build_world``, so this
        is the only place it can be reached from.
        """
        facade = getattr(getattr(world, "loop", None), "turn_memory", None)
        return facade if facade is not None and hasattr(facade, "list_rows") else None

    def memory_list(params: dict[str, Any]) -> dict[str, Any]:
        """The notes themselves, not just their number (M7).

        ``/memory`` answered "43 notes, 33 scenes" and nothing else, which is
        not actionable: the operator could not read what the agent believed,
        could not pin the ones that matter, and could not remove one they had
        taught it wrongly. The store has had ``pin`` and ``delete`` since R3 —
        this is the way to reach them.
        """
        facade = _memory_facade()
        if facade is None:
            return {"notes": [], "enabled": False, "stats": {}}
        rows = []
        for note in facade.list_rows():
            rows.append(
                {
                    "id": note.id,
                    "text": note.text,
                    "kind": str(getattr(note.kind, "value", note.kind)),
                    "scene": note.scene,
                    # M10: which tier the note lives in. Without it a user-tier
                    # note is indistinguishable from a project one, and the
                    # operator has no way to see why it shows up everywhere.
                    "scope": str(getattr(note, "scope", "") or "workspace"),
                    "pinned": bool(note.pinned),
                    "importance": int(note.importance),
                    "evidence": note.evidence,
                    "hits": int(note.hits),
                    "updated": note.updated_time,
                }
            )
        return {"notes": rows, "enabled": True, "stats": facade.status()}

    def memory_pin(params: dict[str, Any]) -> dict[str, Any]:
        facade = _memory_facade()
        note_id = str(params.get("id") or "").strip()
        if facade is None or not note_id:
            return {"ok": False, "reason": "no such memory"}
        pinned = bool(params.get("pinned", True))
        return {"ok": facade.pin(note_id, pinned) is not None, "pinned": pinned}

    def memory_promote(params: dict[str, Any]) -> dict[str, Any]:
        """Move one note from the workspace tier to the user tier (M10).

        The only way in besides ``memory_write(scope="user")``, and the way to
        correct the common case: a preference the model stored while working, or
        one you taught it in a project and then found yourself repeating
        elsewhere.
        """
        facade = _memory_facade()
        note_id = str(params.get("id") or "").strip()
        if facade is None or not note_id:
            return {"ok": False, "reason": "no such memory"}
        return {"ok": facade.promote(note_id) is not None}

    def memory_delete(params: dict[str, Any]) -> dict[str, Any]:
        facade = _memory_facade()
        note_id = str(params.get("id") or "").strip()
        if facade is None or not note_id:
            return {"ok": False, "reason": "no such memory"}
        return {"ok": facade.delete(note_id)}

    def _skills_root() -> Any:
        """The loop's skills registry root, or ``None`` when there is no registry."""
        registry = getattr(getattr(world, "loop", None), "skills", None)
        return getattr(registry, "root", None)

    def skill_pending_list(params: dict[str, Any]) -> dict[str, Any]:
        """Skills the review generated and nobody has approved yet (M8).

        `skill_review` runs on a timer, by default on, and used to write straight
        into `skills/`. A generated instruction is something the model will then
        follow, so it waits in `.pending/` until a human says so.
        """
        from ai_coding.core.skill_review import list_pending

        root = _skills_root()
        if root is None:
            return {"pending": []}
        return {
            "pending": [
                {"name": name, "reason": reason} for name, reason in list_pending(root)
            ]
        }

    def skill_pending_approve(params: dict[str, Any]) -> dict[str, Any]:
        from ai_coding.core.skill_review import approve_skill

        root = _skills_root()
        name = str(params.get("name") or "").strip()
        if root is None or not name:
            return {"ok": False, "reason": "no such pending skill"}
        return {"ok": approve_skill(root, name), "name": name}

    def skill_pending_reject(params: dict[str, Any]) -> dict[str, Any]:
        from ai_coding.core.skill_review import reject_skill

        root = _skills_root()
        name = str(params.get("name") or "").strip()
        if root is None or not name:
            return {"ok": False, "reason": "no such pending skill"}
        return {"ok": reject_skill(root, name), "name": name}

    def config_summary(params: dict[str, Any]) -> dict[str, Any]:
        ai_cfg = getattr(world.config, "ai", None)
        return {
            "model": str(getattr(world.config, "default_model", "") or ""),
            "workspace": world.workspace,
            "maxContextTokens": resolve_context_window(world.config),
            "maxToolIterations": int(getattr(ai_cfg, "max_tool_iterations", 0) or 0),
            "stopPolicy": str(getattr(ai_cfg, "stop_policy", "") or ""),
        }

    def session_stats(params: dict[str, Any]) -> dict[str, Any]:
        """Token usage for the sidebar/status bar (M4).

        ``context_tokens`` explains why a compaction zeroes the reading.
        """
        session = state.session
        tokens = context_tokens(session)
        return {
            "tokens": tokens,
            "maxContextTokens": resolve_context_window(world.config),
            # P5 (18 §10.5): the posture axes ride the stats too, always
            # explicit — the frontend casts blindly and treats a missing key
            # as "old backend", so an always-present shape keeps the two
            # sides from drifting on what "absent" means.
            "mode": str(getattr(session, "mode", "normal") or "normal"),
            "readonly": bool(getattr(session, "readonly", False)),
        }

    def mcp_connect(params: dict[str, Any]) -> dict[str, Any]:
        """M3: the failed row's action — try this server again, now.

        The manager owns the retry loop, so this only wakes it: an immediate
        attempt instead of whatever backoff pause it was sitting in.  False is
        reported rather than raised, because the panel is asking about a row
        the operator can see, and "that server cannot be connected" is an
        answer, not a fault.
        """
        # ``reason``, not ``error``: a top-level ``error`` key is the JSON-RPC
        # error field, and a front end that reads one gets a decode failure
        # instead of the answer (the convention session.delete already follows).
        manager = getattr(world, "mcp", None)
        if manager is None:
            return {"ok": False, "reason": "no MCP manager in this world"}
        name = str(params.get("server") or "")
        if not name:
            return {"ok": False, "reason": "missing server name"}
        if not manager.request_connect(name):
            return {"ok": False, "reason": f"no connectable MCP server named '{name}'"}
        return {"ok": True}

    def mcp_login(params: dict[str, Any]) -> dict[str, Any]:
        """M4: sign a remote server in (the panel's needs_auth action).

        It waits on a human — browser, consent screen, redirect back — so it
        cannot hold this call: the reply means "started", and what happened
        arrives the way every other MCP state change does, as events the dialog
        reads.  Same shape as ``session.turn``'s long work.
        """
        manager = getattr(world, "mcp", None)
        if manager is None:
            return {"ok": False, "reason": "no MCP manager in this world"}
        name = str(params.get("server") or "")
        if not name:
            return {"ok": False, "reason": "missing server name"}

        async def run_login() -> None:
            try:
                await manager.login(name)
            except Exception as exc:  # noqa: BLE001 — a bug must not hang the UI
                _log.exception("mcp login failed outside the manager")
                service.notify(
                    "event",
                    {
                        "type": "mcp.login.finished",
                        "data": {
                            "server": name,
                            "ok": False,
                            "reason": str(exc)[:300],
                        },
                    },
                )

        future = service.spawn(run_login())
        future.add_done_callback(lambda _f: None)
        return {"ok": True}

    def mcp_add(params: dict[str, Any]) -> dict[str, Any]:
        """M5: connect a server the wizard just collected (the guided add).

        Adopt first, persist second: adopting validates against the live set
        (a duplicate name, a missing command) and costs nothing to refuse,
        while a failed write afterwards must be *reported* — a server that runs
        this session but silently vanishes on the next start is the failure mode
        this ordering exists to avoid.
        """
        manager = getattr(world, "mcp", None)
        if manager is None:
            return {"ok": False, "reason": "no MCP manager in this world"}
        from ai_coding.config.loader import ConfigLoader
        from ai_coding.config.models import MCPServerConfig

        name = str(params.get("name") or "").strip()
        kind = str(params.get("type") or "local")
        if not name:
            return {"ok": False, "reason": "a server needs a name"}
        # The name is a tool prefix: ``server__tool`` is the name the gate, the
        # catalog and the model all spell, so a space in it would produce tool
        # names no provider accepts.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            return {
                "ok": False,
                "reason": "a server name may only hold letters, digits, - and _",
            }
        # A manual token becomes a bearer header and nothing else. It is never
        # echoed into a refusal, and it never becomes an empty "Bearer " when
        # the operator skipped the step — an empty bearer reads as *bad*
        # credentials to a server, which is worse than sending nothing.
        token = str(params.get("token") or "").strip()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        server = MCPServerConfig(
            name=name,
            type="remote" if kind == "remote" else "local",
            command=[str(part) for part in params.get("command") or []],
            url=str(params.get("url") or ""),
            headers=headers,
            auto_approve=bool(params.get("auto_approve")),
        )
        verdict: dict[str, Any] = manager.adopt(server)
        if not verdict.get("ok"):
            return verdict
        # The supervisor is a task, and this handler runs on the IPC serve
        # thread: spawning it is the same marshalling ``session.turn`` uses,
        # and the reason adopt() does not create it itself.
        service.spawn(manager.start_supervisor(server))
        # The trust answer belongs to the live gate as well as the config: the
        # gate was built at startup, so a server added since would otherwise
        # prompt anyway until the next start.
        if server.auto_approve:
            gate = getattr(getattr(world.loop, "dispatcher", None), "gate", None)
            if gate is not None and hasattr(gate, "trust_server"):
                gate.trust_server(server.name)
        path = getattr(world, "config_path", None)
        if not path:
            return {"ok": True, "reason": "connected, but no config file to persist beside"}
        try:
            saved = ConfigLoader.append_mcp_server(path, server)
        except OSError as exc:
            return {
                "ok": True,
                "reason": f"connected, but not persisted: {exc}",
                "saved": None,
            }
        return {"ok": True, "saved": str(saved)}

    def mcp_logout(params: dict[str, Any]) -> dict[str, Any]:
        """M4: forget a remote server's token; it returns to needs_auth."""
        manager = getattr(world, "mcp", None)
        if manager is None:
            return {"ok": False, "reason": "no MCP manager in this world"}
        name = str(params.get("server") or "")
        if not name:
            return {"ok": False, "reason": "missing server name"}
        verdict: dict[str, Any] = manager.logout(name)
        return verdict

    def mcp_list(params: dict[str, Any]) -> dict[str, Any]:
        """Rows for the MCP dialog: what was configured and what came of it.

        The manager is the only place that knows both halves — the config is
        what was asked for, its owners are what happened — so it answers with
        the rows rather than the dialog reading a private list (MCP-MCP §2.2).
        """
        manager = getattr(world, "mcp", None)
        if manager is None:
            return {"servers": [], "enabled": False}
        # The master switch rides along with the rows: a panel that cannot see
        # it offers "connect a new server" against a subsystem that is off, and
        # the operator only learns at the end of the wizard (M5 follow-up).
        return {
            "servers": manager.servers(),
            "enabled": bool(getattr(manager, "enabled", True)),
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
        "session.resume": session_resume,
        "session.interrupt": session_interrupt,
        "session.command": session_command,
        "session.delete": session_delete,
        "model.set": model_set,
        "permissions.state": permissions_state,
        "skills.list": skills_list,
        "slash.list": slash_list,
        "files.find": files_find,
        "memory.list": memory_list,
        "memory.pin": memory_pin,
        "memory.delete": memory_delete,
        "memory.promote": memory_promote,
        "skill.pending.list": skill_pending_list,
        "skill.pending.approve": skill_pending_approve,
        "skill.pending.reject": skill_pending_reject,
        "config.summary": config_summary,
        "session.stats": session_stats,
        "mcp.list": mcp_list,
        "mcp.connect": mcp_connect,
        "mcp.login": mcp_login,
        "mcp.logout": mcp_logout,
        "mcp.add": mcp_add,
        "shutdown": shutdown_handler,
    }
