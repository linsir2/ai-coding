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
import os
import re
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

from ai_coding.attachments import save_data_url
from ai_coding.config.loader import ConfigLoader
from ai_coding.config.models import _coerce_model, resolve_context_window
from ai_coding.config.secrets import secret_env_names
from ai_coding.core.agent_loop import resolve_model_key
from ai_coding.core.events import ResumeStalled
from ai_coding.domain.agents import is_plan_agent
from ai_coding.domain.session import InboxItem, _message_to_dict
from ai_coding.infra.time_utils import now_utc
from ai_coding.infra.vcs import git_branch
from ai_coding.providers import GENERIC, PROVIDERS, api_key_env
from ai_coding.server.bridge import EventBridge, Notify
from ai_coding.server.display import provider_from_base_url
from ai_coding.server.service import IpcService
from ai_coding.server.world import AppWorld

_log = logging.getLogger(__name__)

__all__ = ["HandlerTable", "make_handlers"]


HandlerTable = dict[str, Callable[[dict[str, Any]], Any]]


def _active_engine(world: AppWorld) -> Any:
    """The engine the loop is using right now.

    ``world.engine`` is the boot-time object; ``AgentLoop.set_model`` replaces
    ``loop.engine``, and a switch typed by hand (``/model x``) never touches the
    world.  One read, so ``initialize`` / ``model.set`` / ``model.list`` cannot
    disagree about which model is active.
    """
    return getattr(world.loop, "engine", None) or world.engine


def _install_providers(
    world: AppWorld,
    service: IpcService,
    *,
    get_session: Callable[[], Any] | None = None,
) -> None:
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
            gate = getattr(dispatcher, "gate", None)
            if gate is not None and hasattr(gate, "grant_key"):
                grantable = gate.grant_key(tool_name, params) is not None
            else:
                # Legacy/doubles without a gate keep the pre-M4 lexical rule;
                # production always reaches the workspace-aware method above.
                from ai_coding.security.permission_gate import grant_key

                grantable = grant_key(tool_name, params) is not None

            answer = str(
                await service.call(
                    "approval.request",
                    {
                        "toolName": tool_name,
                        "params": params,
                        "reason": reason,
                        # P0-4A: whether an "always" would be remembered at all.
                        # ``grant_key`` is the recorder's own rule -- the same call
                        # ``PermissionGate.grant`` makes -- so the answer travels
                        # instead of the panel re-implementing a security rule in
                        # TypeScript.  ``None`` means policy excludes the tool or
                        # the arguments name no single resource.
                        "grantable": grantable,
                    },
                )
            )
            # C-3: a refusal may carry the operator's own words.  They are not
            # metadata on the call -- they are the user speaking, so they go on
            # the user lane (a next-step row in the session's inbox table) and
            # the loop delivers them at the next iteration boundary, in the same
            # request as the refusal they answer: this runs before the dispatcher
            # resumes.  Only the decision token goes on to the shared parser,
            # which is why ``ApprovalCallback``, ``ApprovalResult`` and the
            # REPL's vocabulary are untouched.  A note the table refuses (a line
            # starting with "/") is logged rather than rewritten -- these words
            # are the operator's, not ours to edit.
            decision, separator, note = answer.partition(NOTE_SEPARATOR)
            if separator and note.strip():
                if note.strip().startswith("/"):
                    _log.info("approval note not queued (looks like a command): %r", note[:80])
                else:
                    session = get_session() if get_session is not None else None
                    if session is None:
                        _log.info("approval note dropped (no live session): %r", note[:80])
                    else:
                        session.inbox.append(
                            InboxItem(
                                id=uuid.uuid4().hex[:8], text=note.strip(),
                                target="next-step", created_at=now_utc(),
                            )
                        )
                        _persist_session(world, session)
            return decision

        set_approval_provider(approval_provider)

    prompt = getattr(world, "user_prompt", None)
    set_prompt_provider = getattr(prompt, "set_provider", None)
    if set_prompt_provider is not None:

        async def question_provider(
            question: str,
            options: list[str],
            detail: str = "",
            intent: str = "",
            data: dict[str, Any] | None = None,
        ) -> str:
            raw = await service.call(
                "user_prompt.ask",
                {
                    "question": question,
                    "options": list(options),
                    # 19 §3.2: always explicit; plain readers ignore the keys.
                    "detail": detail,
                    "intent": intent,
                    # R04: the review card's structured rows ride the same
                    # "always explicit" rule ({} for a plain question).
                    "data": dict(data or {}),
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

#: M-C3: consecutive boot-to-boot resumes that made no progress before the task
#: is disarmed.  One is not enough -- a crash can land very early in
#: ``resume_turn``, before any round opened.
_NO_PROGRESS_RESUMES = 2


def resume_fingerprint(task: Any, session: Any) -> tuple[int, int, int, int]:
    """The boot-to-boot fingerprint of an armed task (M-C3).

    Four facts, every one of which a *real* resume moves: the rounds the driver
    opened, the tokens the episode spent, the real (non-synthetic) messages and
    the steps the plan settled.  A synthetic ``[resume]`` note is deliberately
    excluded -- ``resume_turn`` appends one whenever a pending approval is
    re-decided, and counting it would read a genuine stall as progress.
    """
    rounds = int(getattr(task, "rounds_run", 0) or 0)
    tokens = int(getattr(task, "tokens_used", 0) or 0)
    messages = sum(
        1
        for message in getattr(session, "messages", ()) or ()
        if not bool(getattr(message, "synthetic", False))
    )
    steps = len(getattr(session, "step_deliveries", ()) or ())
    return (rounds, tokens, messages, steps)

#: C-3: how a refusal carries the operator's own words ("no::改用 ls -la").
#: The panel builds it (util/permission.ts), and it is lifted back off here --
#: before the shared parser sees anything, because that parser serves two front
#: ends and speaks only the CLI vocabulary.  Two literals, one protocol.
NOTE_SEPARATOR = "::"


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


#: How many turns a finished background job may open on its own before the
#: harness goes quiet and waits for the human (D-1c).  dsh ships this budget
#: uncapped by default and oc always resumes; three is the conservative reading
#: of the same design, because a woken turn can start the job that wakes it
#: again and nobody is watching the meter.
WAKE_BUDGET = 3


def wake_decision(budget_left: int, turns_in_flight: int) -> bool:
    """Whether a finished job may open a turn of its own (D-1c).

    Two refusals, and each has its own reason:

    * no budget left -- a woken turn can start the job whose completion wakes it
      again, so the chain has to end somewhere;
    * a turn is already running -- it drains the notice at its own iteration
      boundary, and waking it again would put the same line in the context twice
      while spending a turn to do it.
    """
    return budget_left > 0 and turns_in_flight == 0


def make_handlers(
    world: AppWorld,
    service: IpcService,
    *,
    shutdown: Callable[[], None] | None = None,
) -> HandlerTable:
    """Build the request table bound to one world + one service."""
    state = _WorldState()
    bridge = EventBridge(lambda method, params: service.notify(method, params))
    _install_providers(world, service, get_session=lambda: state.session)
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
        resume_note = ""
        # P0-3: whether a run actually starts.  ``resumed`` alone cannot say so:
        # the breaker branch answers it too and starts nothing, and a shell that
        # sat busy over a run nobody launched would wait forever.
        resuming = False
        if pending is not None:
            candidate = world.sessions.load(pending.session_id) or pending
            candidate_task = getattr(candidate, "task", None)
            if (
                candidate_task is not None
                and getattr(candidate_task, "armed", False)
                and getattr(candidate_task, "armed_profile", "") in ("plan", "goal")
            ):
                boot_session = candidate
        if boot_session is not None:
            session = boot_session
            state.session = session
            state.session_id = session.session_id
            task = session.task
            # D-4: the note names what was actually interrupted; "plan" and
            # "goal" are different tasks to the person reading it.
            profile = str(getattr(task, "armed_profile", "") or "plan")
            label = "目标" if profile == "goal" else "计划"
            # M-C3: compare against the fingerprint the last resume left.  The
            # comparison runs before either breaker so a stall is counted even
            # when the older cap is what finally disarms.
            now = resume_fingerprint(task, session)
            if getattr(task, "resume_fingerprint", None) is not None:
                if now == tuple(task.resume_fingerprint):
                    task.no_progress_resumes = (
                        int(getattr(task, "no_progress_resumes", 0) or 0) + 1
                    )
                else:
                    task.no_progress_resumes = 0
            if int(getattr(task, "resume_count", 0) or 0) >= _MAX_BOOT_RESUMES:
                task.disarm()
                _persist_session(world, session)
                resume_note = (
                    f"[{profile}] 自动续跑已达上限（{_MAX_BOOT_RESUMES} 次），"
                    f"{label}任务已暂停；检查后继续对话"
                    + ("或重新立案（/goal 或让模型 start_goal）。" if profile == "goal"
                       else "或重新进入 Plan 模式。")
                )
            elif int(getattr(task, "no_progress_resumes", 0) or 0) >= _NO_PROGRESS_RESUMES:
                task.disarm()
                _persist_session(world, session)
                # M-C3: the trace recorder is not in the observer braid until a
                # turn begins, and this branch begins none -- bind it first or
                # the event lands under root/"unknown" (trace.py's
                # session_dir("")).
                bind_session = getattr(world.loop, "bind_session", None)
                if callable(bind_session):
                    bind_session(session)
                before = tuple(getattr(task, "resume_fingerprint", None) or ())
                after = resume_fingerprint(task, session)
                emit = getattr(getattr(world.loop, "observer", None), "on_event", None)
                if callable(emit):
                    emit(
                        ResumeStalled(
                            resumes=int(getattr(task, "resume_count", 0) or 0),
                            no_progress=int(task.no_progress_resumes),
                            before=before,
                            after=after,
                        )
                    )
                resume_note = (
                    f"[{profile}] 连续 {_NO_PROGRESS_RESUMES} 次自动续跑均无进展，"
                    f"{label}任务已暂停；检查后继续对话"
                    + ("或重新立案（/goal 或让模型 start_goal）。" if profile == "goal"
                       else "或重新进入 Plan 模式。")
                )
            else:
                # 可达+有痕批次: the note rides the initialize response, not a
                # console.line -- events emitted while the shell is still on
                # Home are lost, and this one must be seen.
                resume_note = (
                    f"[{profile}] 应用重启：自动续跑未完成的{label}任务"
                    f"（第 {int(getattr(task, 'resume_count', 0) or 0) + 1} 次）。"
                    "执行过程会实时显示在本会话；停止请按 esc。"
                )
                # R07: boot resumes *execution*, so the parent goes back to
                # build first — keeping PLAN here left the status bar and the
                # prompt posture in planning while the runner executed (F-46).
                # Persist before the run so a crash mid-resume still reads
                # build.
                # M1b: one write path, so the reset also records its history
                # row and clears the durable sidecar.
                world.loop.commit_posture(
                    session,
                    agent="build",
                    readonly=False,
                    reason="boot-resume",
                    observer=bridge,
                )
                _persist_session(world, session)
                # M-C3: take the fingerprint *after* the persist, so it counts
                # the messages that actually survived ``ensure_tool_pairing``.
                # Taken before, a carrier dropped on the way to disk would read
                # as progress on the next boot and hide a real stall.
                task.resume_fingerprint = resume_fingerprint(task, session)
                _persist_session(world, session)
                _launch_boot_resume(session)
                resuming = True
        else:
            session = world.sessions.create()
            state.session = session
            state.session_id = session.session_id
        model = getattr(_active_engine(world), "model_config", None)
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
            # 可达+有痕批次: the shell opens the resumed session and renders
            # the note (approvals and the live run become reachable there).
            "resumed": boot_session is not None,
            "resumeNote": resume_note,
            "resuming": resuming,
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
        # R05: the plan card is repainted from the recovered task — the card
        # used to be painted by live plan.updated events only, so a reopened
        # session lost it (F-48).  Empty task reads as None (same rule as the
        # stored form: a task with no items is not written).
        task = getattr(session, "task", None)
        task_payload = task.to_dict() if task is not None and task.items else None
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
            # M1b T1b: the durable switch history, so a replayed transcript
            # draws the same "Switched agent" rows the live one showed.
            "agentHistory": [dict(row) for row in session.agent_history],
            "task": task_payload,
        }

    def _launch_turn(run: Callable[[], Coroutine[Any, Any, None]]) -> int:
        """Spawn a turn coroutine and track it (08).

        Shared by ``session.prompt`` and ``session.resume``: the guarded shape
        -- cancellation passes through, an outside failure becomes the
        ``turn.finished`` error event -- is the contract both callers need, and
        writing it twice is how the two drift apart.
        """
        state.turn_id += 1
        turn_id = state.turn_id
        # D7: the bridge stamps the loop's live TurnFinished with this id.  The
        # ack already carries it, so both ends of the turn agree on its name.
        bridge.turn_id = turn_id

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
                            # the only ending a broken runner gets (D7)
                            "turnId": turn_id,
                        },
                    },
                )

        future = service.spawn(guarded())
        state.turn_futures.add(future)
        future.add_done_callback(_watch_turn)
        return turn_id

    def _notify_terminal_turn(
        session: Any,
        *,
        status: str,
        reason: str,
        action: str,
        history_before: int,
        turn_id: int,
        reload_in_place: bool = False,
    ) -> None:
        """The one end a multi-turn runner owes the client (P0-3).

        ``session_command`` has always emitted its own terminal event after
        ``handle_input`` returns, because every inner turn left its
        ``TurnFinished`` on the wire as a *step*.  A boot resume has the same
        shape (a resumed round plus one turn per plan step) and owed the client
        nothing: it borrowed ``_launch_turn``'s wrapper, which only reports an
        unhandled exception, so a run that finished normally left ``busy`` set
        forever.  One event, one definition -- the fields below are exactly the
        ones the command path has always sent.
        """
        after = len(getattr(session, "messages", ()) or ())
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
                    "action": action,
                    # D7: the turn this ending belongs to (the ack's id).
                    "turnId": turn_id,
                    # the live session after the run (a NEW_SESSION action has
                    # switched it)
                    "sessionId": state.session_id,
                    # A switched session must re-project; a same-session rewrite
                    # (/compact) only reports the fact — except /undo and /redo,
                    # which *rewrite the history in place* and therefore do need
                    # the client to re-project (r17/T1).  The text rides along so
                    # the composer can get the undone user message back.
                    "reload": state.session is not session or reload_in_place,
                    "undo": (
                        world.loop.last_undo_for(state.session_id)
                        if reload_in_place
                        else None
                    ),
                    "messagesBefore": history_before,
                    "messagesAfter": after,
                    "compacted": (
                        {"before": history_before, "after": after}
                        if state.session is session
                        and after < history_before
                        else None
                    ),
                },
            },
        )

    def _launch_boot_resume(session: Any) -> None:
        _launch_armed_run(session, boot=True)

    def _maybe_launch_goal_run(session: Any) -> None:
        """D-4: a ``start_goal`` call armed the task mid-turn; its run starts now.

        The plan's approval path dispatches ``/plan run`` from the frontend
        (the approval click is a UI event); a model's own arming has no UI
        event, so the dispatch lives at this turn boundary.  Esc disarms
        before cancelling (``_interrupt_running_turns``), so an interrupted
        arming turn never starts a run, and a bare user turn after a stopped
        goal re-arms nothing by itself.
        """
        task = getattr(session, "task", None)
        if task is None or not getattr(task, "armed", False):
            return
        if str(getattr(task, "armed_profile", "") or "") != "goal":
            return
        if bridge.command_run:
            return
        _launch_armed_run(session, boot=False)

    def _launch_armed_run(session: Any, *, boot: bool) -> None:
        """Drive an armed task with its profile's runner (11-spec §7.2, D-4).

        ``boot=True`` is the restart path: the interrupted round first (when
        one is open), the resume counted before anything runs.  ``boot=False``
        is the post-turn dispatch for a goal the model just started (a
        ``start_goal`` call arms mid-turn; its run starts after that turn).

        The run goes through ``PlanRunner.run`` / ``GoalRunner.run``, not the
        driver directly -- those entries carry the whole armed lifecycle (arm /
        roundsRun / disarm persistence), which a bare driver call would
        bypass.
        """

        # Filled by ``_launch_turn`` below, before the task first runs: this
        # handler is synchronous, so the coroutine cannot start earlier.  The
        # run's one terminal event names the turn it ends (D7).
        turn_id = 0

        async def run_resume() -> None:
            task = getattr(session, "task", None)
            # Count BEFORE resuming: if this resume crashes too, the count is
            # already on disk and the breaker keeps converging.
            if boot and task is not None:
                task.resume_count = int(getattr(task, "resume_count", 0) or 0) + 1
                _persist_session(world, session)
            history_before = len(getattr(session, "messages", ()) or ())
            console = IpcConsole(
                lambda method, message: service.notify(method, message), bridge.on_delta
            )
            # P0-3: the same envelope ``session_command`` uses.  Every inner turn
            # here is a *step* of one run -- the resumed round plus one turn per
            # plan step -- so the bridge rewrites their TurnFinished to
            # ``turn.step`` and the run itself ends once, below.  The status is
            # settled here rather than in ``_launch_turn``'s wrapper so the run
            # emits exactly one terminal event on every path: an exception that
            # escaped would arrive as that wrapper's event with ``command_run``
            # already reset, i.e. as a second end.
            bridge.command_run = True
            status, reason = "completed", ""
            try:
                if boot and session.open_turn is not None:
                    await world.loop.resume_turn(
                        session, on_delta=bridge.on_delta, observer=bridge
                    )
                if getattr(task, "armed", False):
                    # D-4: the profile decides the runner.  "plan" is the
                    # human-approved plan run; "goal" is the model's own
                    # objective, one ordinary turn per round.
                    profile = str(getattr(task, "armed_profile", "") or "plan")
                    if profile == "goal":
                        from ai_coding.core.goal_runner import GoalRunner

                        await GoalRunner(world.loop).run(
                            session, console, on_delta=bridge.on_delta, observer=bridge
                        )
                    else:
                        from ai_coding.core.plan_runner import PlanRunner

                        await PlanRunner(world.loop).run(
                            session, console, on_delta=bridge.on_delta, observer=bridge
                        )
            except asyncio.CancelledError:
                status, reason = "aborted", "interrupted"
            except Exception as exc:  # noqa: BLE001 — a bug must not hang the UI
                _log.exception("boot resume failed outside the dispatcher")
                status, reason = "error", f"{type(exc).__name__}: {exc}"[:300]
            finally:
                bridge.command_run = False
            _notify_terminal_turn(
                session,
                status=status,
                reason=reason,
                action="continue",
                history_before=history_before,
                turn_id=turn_id,
            )

        turn_id = _launch_turn(run_resume)

    def session_prompt(params: dict[str, Any]) -> dict[str, Any]:
        """Start a real turn; events reach the client through the bridge."""
        # The human spoke: a job that ends from here on may wake the session
        # again (dsh's rule -- the budget counts turns opened *since* the last
        # human input).
        wake_left["left"] = WAKE_BUDGET
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
            # A human utterance is not always an ordinary turn: in plan
            # mode it is a planning round (19 §4).  The routing lives in
            # ``run_human_turn`` so the REPL and this front door cannot
            # drift apart -- the TUI sending the words as an ordinary
            # prompt is what made plan mode a read-only flag instead of a
            # planning mode.
            from ai_coding.app.commands import run_human_turn

            console = IpcConsole(
                lambda method, message: service.notify(method, message),
                bridge.on_delta,
            )
            await run_human_turn(
                world.loop,
                session,
                text,
                console=console,
                on_delta=bridge.on_delta,
                observer=bridge,
                # offered only when there is something to offer, so loop
                # doubles that predate r17/T5 stay callable for text turns
                images=tuple(images),
            )
            # D-4: the model may have armed a goal in this turn (start_goal).
            _maybe_launch_goal_run(session)

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

    def _interrupt_running_turns() -> int:
        """Stop whatever turn is in flight, and say how many that was.

        Shared by Esc and by promote: two spellings of "stop what is running"
        must not drift into two behaviours.  The disarm comes first (11-spec
        §8) so an armed plan does not reopen after this round unwinds.
        """
        session = getattr(state, "session", None)
        task = getattr(session, "task", None) if session is not None else None
        if task is not None and getattr(task, "armed", False):
            task.disarm()
            _persist_session(world, session)
        # A stop is not a latch's business (2026-10-17): a wake waiting for the
        # runway must not open a turn right after the user hit Esc -- dsh
        # clears its latch on cancel for the same reason.  The queued content
        # stays durable and rides the next human turn.
        wake_pending["prompt"] = None
        # Two layers, both needed (ported from ui/textual_app.py::
        # action_cancel_turn): the event is the tool batch's fast path (the
        # loop clears it before each batch), the future cancellation reaches
        # a model call.
        world.cancel.set()
        return sum(1 for future in list(state.turn_futures) if future.cancel())

    def session_interrupt(params: dict[str, Any]) -> dict[str, Any]:
        # 11-spec §8: disarm BEFORE cancelling — the driver loop re-checks
        # task.armed between rounds and must not open the next one after this
        # round unwinds.  Best-effort: the round's own close save carries the
        # same in-memory object, so both writes agree.
        return {"ok": True, "interrupted": _interrupt_running_turns()}

    def _next_step_count(session: Any) -> int:
        """How many next-step rows are still waiting -- what the old FIFO's
        length meant to the composer."""
        return sum(1 for item in session.inbox if item.target == "next-step")

    def session_steer(params: dict[str, Any]) -> dict[str, Any]:
        """steering 前端化: a line typed mid-turn goes to the run, not the queue.

        It lands in the session's inbox table as a next-step row (D-2 Phase 2),
        where the loop drains it at its next iteration boundary (R7e-2) -- the
        same place the old in-memory FIFO used to feed.  Commands and blank
        lines are refused by the same rule as before; the caller keeps those
        on its queue path.  ``pending`` counts the next-step rows still
        waiting, which is what the old queue length meant.
        """
        session = state.session
        if session is None:
            raise ValueError("no session initialized")
        text = str(params.get("text", "") or "").strip()
        if not text or text.startswith("/"):
            return {"accepted": False, "pending": _next_step_count(session)}
        session.inbox.append(
            InboxItem(
                id=uuid.uuid4().hex[:8], text=text, target="next-step",
                created_at=now_utc(),
            )
        )
        _persist_session(world, session)
        return {"accepted": True, "pending": _next_step_count(session)}

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
        # A slash command is the human speaking too (the panel's /tasks stop
        # included): the wake budget counts turns opened *since* the human last
        # spoke, whatever entry they used.
        wake_left["left"] = WAKE_BUDGET
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
                _install_providers(world, service, get_session=lambda: state.session)
            _notify_terminal_turn(
                session,
                status=status,
                reason=reason,
                action=action_name,
                history_before=history_before,
                turn_id=turn_id,
                reload_in_place=reload_in_place,
            )
            if action_name == "quit" and shutdown is not None:
                shutdown()

        future = service.spawn(run_command())
        state.turn_futures.add(future)
        future.add_done_callback(_watch_turn)
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

        M-D3: a grant is reported as ``{key, expiresAt}`` -- ``expiresAt`` is the
        epoch deadline, or ``null`` for a permanent one -- and the session-scoped
        one-off answers ride along under ``onceGrants``.  Both are shown because
        an authorisation the operator cannot inspect is one they cannot consent
        to, and a "this once" that looks like a standing grant is the same
        deception one level down.
        """
        from ai_coding.security.approval import AutoApproveCallback

        dispatcher = getattr(world.loop, "dispatcher", None)
        approval = getattr(dispatcher, "approval", None)
        gate = getattr(dispatcher, "gate", None)
        store = getattr(gate, "_store", None)
        from ai_coding.security.permissions import rule_to_dict, rules_from_dicts

        rules, session_errors = rules_from_dicts(
            getattr(state.session, "permissions", ()) or ()
        )
        counts = getattr(gate, "layer_counts", None)
        agent_rules_of = getattr(world.loop, "_agent_rules", None)
        agent_rules = (
            agent_rules_of(state.session) if callable(agent_rules_of) else ()
        )
        layers = counts(rules, agent_rules) if callable(counts) else {}
        errors = [
            *tuple(getattr(getattr(gate, "_rules", None), "errors", ()) or ()),
            *session_errors,
        ]
        records = (
            gate.grant_records()
            if gate is not None and hasattr(gate, "grant_records")
            else []
        )
        once = sorted(getattr(gate, "_once_grants", ()) or ())
        return {
            "policy": "auto" if isinstance(approval, AutoApproveCallback) else "confirm",
            "grants": [
                {"key": key, "expiresAt": expiry} for key, expiry in records
            ],
            "onceGrants": once,
            "storePath": str(getattr(store, "path", "") or ""),
            "rules": {
                "layers": layers,
                "session": [rule_to_dict(rule) for rule in rules],
                "errors": errors,
            },
        }

    def permissions_revoke(params: dict[str, Any]) -> dict[str, Any]:
        """M-D3: forget one grant, by full key or by base name.

        The mutation goes through the gate (memory + store), never the file, so
        ``/permissions revoke`` and the panel's button cannot disagree.
        """
        key = str(params.get("key", "") or "").strip()
        dispatcher = getattr(world.loop, "dispatcher", None)
        gate = getattr(dispatcher, "gate", None)
        if not key:
            return {"ok": False, "reason": "a key is required"}
        if gate is None or not hasattr(gate, "revoke"):
            return {"ok": False, "reason": "no permission gate in this world"}
        removed = bool(gate.revoke(key))
        return {"ok": removed, "key": key}

    def model_set(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        world.loop.set_model(name)
        # The metadata label reads the endpoint of the *active* model, so a
        # switch across providers must report the new one.
        model = getattr(_active_engine(world), "model_config", None)
        return {
            "ok": True,
            "model": name,
            "provider": provider_from_base_url(str(getattr(model, "base_url", "") or "")),
        }

    def model_list(params: dict[str, Any]) -> dict[str, Any]:
        """The live catalogue, with the model the engine is using right now.

        ``current`` is the active engine's ``model_config`` -- identity first,
        ``name`` second -- not the configured default: those are two different
        things, and ``/model`` can move the first without touching the second.
        The api_key is never included; it is a plaintext credential.
        """
        active = getattr(_active_engine(world), "model_config", None)
        models = getattr(world.config, "models", None) or {}
        # Identity first, ``name`` second -- one shared resolver with the loop's
        # own ``_current_model_key``, so the two cannot answer differently.
        current = resolve_model_key(models, active)
        rows = [
            {
                "key": key,
                "name": str(getattr(cfg, "name", "") or key),
                # The same heuristic the status bar uses (``model.set`` and
                # ``initialize``), so the panel and the bar cannot disagree.
                "provider": provider_from_base_url(str(getattr(cfg, "base_url", "") or "")),
                "baseUrl": str(getattr(cfg, "base_url", "") or ""),
                "hasKey": bool(getattr(cfg, "api_key", "")),
            }
            for key, cfg in models.items()
        ]
        return {
            "ok": True,
            "current": current,
            "default": str(getattr(world.config, "default_model", "") or ""),
            "models": rows,
        }

    def model_providers(params: dict[str, Any]) -> dict[str, Any]:
        """The provider table, so the wizard offers exactly what we accept.

        ``hasEnv`` is a snapshot for the UI's default choice; ``model.connect``
        checks the environment again, because it can change in between.
        """
        return {
            "ok": True,
            "providers": [
                {
                    "id": provider_id,
                    "baseUrl": provider.default_base_url,
                    "envVar": provider.api_key_env,
                    "hasEnv": bool(
                        provider.api_key_env and os.environ.get(provider.api_key_env)
                    ),
                }
                for provider_id, provider in PROVIDERS.items()
            ],
        }

    def model_connect(params: dict[str, Any]) -> dict[str, Any]:
        """Register a model, persist it, and make it usable this session (H3).

        Every refusal is ``{ok: false, reason}`` rather than an exception: the
        TUI has no try/except semantics on the wire.  The sidecar keeps the
        api_key **as written** -- ``${VAR}`` stays a reference, so resolving it
        here would write the plaintext secret to disk.
        """
        config_path = getattr(world, "config_path", None)
        if config_path:
            return {
                "ok": False,
                "reason": (
                    f"this run reads {config_path} (--path); add the model there "
                    "instead -- a sidecar write would vanish on the next start"
                ),
            }
        key = str(params.get("model", "") or "").strip()
        if not key:
            return {"ok": False, "reason": "a model name is required"}
        # The loop's table is what ``/model`` switches against; the config's is
        # what the catalogue reports.  They are two dicts, so the collision is
        # checked against the one that would raise.
        switchable = getattr(world.loop, "models", None) or {}
        if key in switchable:
            return {
                "ok": False,
                "reason": (
                    f"model '{key}' already exists; pick another name or "
                    "switch with /model"
                ),
            }
        provider = str(params.get("provider", "") or "").strip()
        base_url = str(params.get("baseUrl", "") or "").strip()
        if provider == GENERIC and not base_url:
            return {"ok": False, "reason": "provider 'generic' needs a baseUrl"}
        api_key_text = str(params.get("apiKey", "") or "").strip()
        env_name = api_key_env(provider)
        if env_name and not os.environ.get(env_name) and not api_key_text:
            return {
                "ok": False,
                "reason": f"export {env_name} or paste a key",
            }
        if not api_key_text and env_name and os.environ.get(env_name):
            api_key_text = "${" + env_name + "}"
        entry: dict[str, Any] = {"provider": provider, "name": key}
        if base_url:
            entry["base_url"] = base_url
        if api_key_text:
            entry["api_key"] = api_key_text
        try:
            cfg = _coerce_model(dict(entry))
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        # Hot-add first: it is the only step that can refuse, and doing it after
        # the write would leave a persisted model that ``/model`` cannot reach.
        world.loop.add_model(key, cfg)
        try:
            ConfigLoader.append_model(key, entry)
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "reason": (
                    f"{key} is usable this session but could not be saved: {exc}"
                ),
            }
        models = getattr(world.config, "models", None)
        if models is None:
            models = {}
            world.config.models = models
        models[key] = cfg
        # The same set the config path would strip: the provider's conventional
        # variable counts even when a literal key was typed, because the child
        # process must not be able to read it either way.
        backend = getattr(world, "sandbox_backend", None)
        add_secret = getattr(backend, "add_secret_env_names", None)
        if callable(add_secret):
            add_secret(sorted(secret_env_names({key: entry})))
        return {
            "ok": True,
            "key": key,
            "provider": provider,
            "name": key,
            "hasKey": bool(cfg.api_key),
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

    def session_inbox(params: dict[str, Any]) -> dict[str, Any]:
        """The operator's queued input as data, not as composer state (D-2).

        One table with two targets -- ``next-turn`` runs after this turn,
        ``next-step`` joins it -- instead of the two in-memory queues that agreed
        on nothing and survived nothing.  Every mutation persists through the
        session save, so a restart keeps what the operator meant to ask next.
        """
        session = state.session
        if session is None:
            raise ValueError("no session initialized")
        op = str(params.get("op", "list") or "list")
        if op == "list":
            return {"ok": True, "rows": [item.to_dict() for item in session.inbox]}
        if op == "add":
            text = str(params.get("text", "") or "").strip()
            if not text:
                return {"ok": False, "reason": "empty text is not a queued line"}
            target = str(params.get("target", "next-turn") or "next-turn")
            if target not in ("next-turn", "next-step"):
                return {"ok": False, "reason": f"unknown target {target!r}"}
            item = InboxItem(
                id=uuid.uuid4().hex[:8], text=text, target=target,
                created_at=now_utc(),
            )
            session.inbox.append(item)
            _persist_session(world, session)
            return {"ok": True, "id": item.id}
        if op == "promote":
            # oc's projectDeliveryChanged in miniature: only the target moves,
            # never the text.  Then, because a cancelled turn is a dead turn in
            # this harness (CancelledError -> _close_turn), the flip alone would
            # leave its own row waiting for the next human message -- the exact
            # opposite of what the button means.  So a promote also stops what
            # is running and chains a wake that consumes the row, on the shared
            # budget: an interrupt without the wake strands the intent, and a
            # wake without the interrupt talks over a turn that is still going.
            row_id = str(params.get("id", "") or "").strip()
            row = next((item for item in session.inbox if item.id == row_id), None)
            if row is None:
                return {"ok": False, "reason": f"no inbox row {row_id!r}"}
            row.target = "next-step"
            _persist_session(world, session)
            interrupted = _interrupt_running_turns()
            woke = _open_promote_wake(row.text)
            return {"ok": True, "id": row.id, "interrupted": interrupted, "woke": woke}
        if op in ("edit", "delete"):
            row_id = str(params.get("id", "") or "").strip()
            row = next((item for item in session.inbox if item.id == row_id), None)
            if row is None:
                return {"ok": False, "reason": f"no inbox row {row_id!r}"}
            if op == "delete":
                session.inbox = [item for item in session.inbox if item.id != row_id]
            else:
                text = str(params.get("text", "") or "").strip()
                if not text:
                    return {"ok": False, "reason": "empty text is not a queued line"}
                row.text = text
            _persist_session(world, session)
            return {"ok": True, "id": row.id}
        return {"ok": False, "reason": f"unknown op {op!r}"}

    def session_jobs(params: dict[str, Any]) -> dict[str, Any]:
        """The workspace's background jobs, for the panel (D-1b).

        A snapshot, not a subscription: ``job.changed`` carries every later
        change, and this covers the client that connected after one -- which is
        the same contract oc's shell list uses, and one fewer thing for the
        frontend to keep in sync.
        """
        registry = getattr(world, "jobs", None)
        if registry is None:  # a world built without jobs says so
            return {"jobs": [], "available": False}
        return {"jobs": registry.snapshot(), "available": True}

    def _session_budget(session: Any) -> dict[str, Any] | None:
        """The session-level budget for the sidebar's session card (M4b).

        Same source and same boundary as ``/budget``: the trace summary plus
        the recorder's prices.  Without a recorder (tracing off), before any
        turn ran, or with nothing recorded, there is nothing honest to show
        and the card hides the line.
        """
        from ai_coding.app.trace_text import budget_figures

        recorder = getattr(world.loop, "trace", None)
        store = getattr(recorder, "store", None)
        session_id = str(getattr(session, "session_id", "") or "")
        if store is None or not session_id:
            return None
        if not store.summary_path(session_id).is_file():
            return None
        figures = budget_figures(
            store.read_summary(session_id),
            price=getattr(recorder, "pricing", None),
            prices=getattr(recorder, "pricings", None),
        )
        if not figures["tokens"] and figures["cost"] is None:
            return None
        return figures

    def session_stats(params: dict[str, Any]) -> dict[str, Any]:
        """Token usage for the sidebar/status bar (M4).

        ``context_tokens`` explains why a compaction zeroes the reading.
        M4b (2026-10-18): the sidebar's session card also reads the facts the
        transcript never shows -- how many turns ran, when the session began,
        and the session budget from the same figures ``/budget`` renders.
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
            # T7: the current agent id (None before the first session exists);
            # the frontend hydrates SessionState.agent from this on mount.
            "agent": getattr(session, "agent", None),
            "turnCount": int(getattr(session, "turn_count", 0) or 0),
            "createdTime": str(getattr(session, "created_time", "") or ""),
            "budget": _session_budget(session),
        }

    def session_agents(params: dict[str, Any]) -> dict[str, Any]:
        """The switchable agents for the picker and shift+tab (T7).

        The visible-primary subset of ``world.loop.agents``: ``visible()``
        still carries subagents (delegation targets, not switch targets), and
        ``primary()`` would include hidden ones.  Sorted by id for stability.
        """
        catalog = getattr(getattr(world, "loop", None), "agents", None)
        if catalog is None:
            return {"agents": []}
        rows = [
            {
                "id": spec.id,
                "name": spec.name,
                "kind": spec.kind,
                "source": spec.source,
                "description": spec.description,
            }
            for spec in catalog.visible()
            if spec.kind == "primary"
        ]
        rows.sort(key=lambda row: str(row["id"]))
        return {"agents": rows}

    def session_agent_select(params: dict[str, Any]) -> dict[str, Any]:
        """Switch the live session's agent (T7).

        A thin adapter over ``world.loop.request_posture``: the four-state
        machine stays the only state machine.  Committed/cancelled/noop
        persist through the closer-or-service save; queued leaves the durable
        write to the pending-posture sidecar that already owns crash recovery.
        """
        session = state.session
        if session is None:
            raise ValueError("no session initialized")
        agent_id = str(params.get("id", "") or "")
        catalog = getattr(getattr(world, "loop", None), "agents", None)
        spec = catalog.get(agent_id) if catalog is not None else None
        # ``reason``, not ``error``: a top-level ``error`` key is the
        # JSON-RPC error field, and the client parses it as one (the same
        # convention session.delete/mcp.connect follow).
        if spec is None:
            return {"ok": False, "reason": "unknown_agent"}
        if spec.kind != "primary":
            return {"ok": False, "reason": "not_primary"}
        result = world.loop.request_posture(
            session,
            agent=agent_id,
            readonly=is_plan_agent(agent_id),
            reason="agent.select",
            observer=bridge,
        )
        status = str(getattr(result, "value", result)).lower()
        if status != "queued":
            _persist_session(world, session)
        return {"ok": True, "status": status}

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

    # ---- D-1c: a finished job may open a turn ------------------------------
    wake_left = {"left": WAKE_BUDGET}
    #: The latch for a wake refused while a turn was in flight (2026-10-17).
    #: One slot on purpose: the content lives in the queues and the prompt
    #: only says to look, so a later request replaces an earlier one and the
    #: replay opens one turn either way (dsh's latch is one bit, same reason).
    wake_pending: dict[str, Any] = {"prompt": None}

    async def _wake_turn(session: Any, text: str) -> None:
        """The turn nobody asked for, opened because something finished.

        The prompt is deliberately generic: the notice (or the promoted row)
        is already queued and the turn's own iteration boundary drains it, so
        this line only has to give the model a reason to look.  Phrasing the
        outcome here would state it twice, and the second statement is the one
        that could drift.

        The bridge rides along like every other turn (2026-10-18): without it
        ``_bind_observer`` recomposed the braid off the construction-time base
        -- Null -- whenever tracing was on, so the wake turn streamed to nobody
        and every background event until the next human turn went nowhere the
        operator could see.
        """
        try:
            await world.loop.process_input(
                session, text, on_delta=bridge.on_delta, observer=bridge
            )
        except Exception:  # noqa: BLE001 — a wake that fails is not a crashed app
            _log.exception("wake turn failed")

    def _request_wake(prompt: str) -> bool:
        """Open a wake turn if the budget allows; True when one was opened.

        Shared by job completions and promote: a wake is a wake no matter who
        asked for it, so they spend the same budget.  The caller decides the
        prompt; the budget decides whether it may run.

        Refusal is not always a loss (2026-10-17): a turn in flight may be
        past its last drain boundary -- its tail, where the memory harvest
        runs for seconds -- and then nobody will ever read the queue this
        wake was for.  That refusal latches the wake for replay at the runway
        (``_replay_pending_wake``); an exhausted budget is the chain breaker
        and latches nothing.
        """
        if state.session is None:
            return False
        if wake_decision(wake_left["left"], len(state.turn_futures)):
            wake_pending["prompt"] = None
            wake_left["left"] -= 1
            _launch_turn(lambda: _wake_turn(state.session, prompt))
            return True
        wake_pending["prompt"] = (
            prompt if (state.turn_futures and wake_left["left"] > 0) else None
        )
        return False

    def _wake_has_deliverable() -> bool:
        """Is there anything a woken turn would actually see?

        dsh's ``inbox.hasPending`` guard, spelled for our two queues: the
        notice lane and the session inbox's next-step rows.  A replay for an
        already-drained queue would spend a model call saying "look" at
        nothing.
        """
        if world.notices is not None and world.notices.pending > 0:
            return True
        session = state.session
        return bool(
            session is not None
            and any(item.target == "next-step" for item in session.inbox)
        )

    def _replay_pending_wake() -> None:
        """Give a latched wake its turn, now that no turn is in flight.

        Called from every turn's done callback (``_watch_turn``), which is the
        one moment the runway is empty -- event-driven on purpose; polling is
        what ``_open_promote_wake`` already does for its own, narrower case,
        and the two must not merge into one waiter.
        """
        prompt = wake_pending["prompt"]
        if prompt is None or state.turn_futures or state.session is None:
            return
        if not _wake_has_deliverable():
            wake_pending["prompt"] = None
            return
        _request_wake(prompt)

    def _watch_turn(future: Future[Any]) -> None:
        """Drop a finished turn; then the runway is empty -- replay if latched."""
        state.turn_futures.discard(future)
        try:
            _replay_pending_wake()
        except Exception:  # noqa: BLE001 — a callback must not eat the loop's log
            _log.debug("latched wake replay failed", exc_info=True)

    def _open_promote_wake(row_text: str) -> bool:
        """Wake for a promoted row: now if idle, after the dying turn if not.

        Returns True only when a wake turn was opened by this call.  A running
        turn cannot be joined -- its session object is being closed underneath
        -- so the waiter polls for an empty future set (bounded: a turn that
        will not die in thirty seconds is not going to, and a second turn on
        the same session would interleave reads and writes).  Giving up is
        logged, not silent: the row stays flipped and durable either way.
        """
        clipped = " ".join(str(row_text).split())
        if len(clipped) > 60:
            clipped = clipped[:60] + "…"
        prompt = f"[notice] 你把这一条提前了——查看并继续：{clipped}"
        if not state.turn_futures:
            return _request_wake(prompt)

        async def _wait_settle_then_wake() -> None:
            for _ in range(300):
                if not state.turn_futures:
                    break
                await asyncio.sleep(0.1)
            else:
                _log.warning("promote wake gave up waiting for the dying turn")
                return
            _request_wake(prompt)

        try:
            service.spawn(_wait_settle_then_wake())
        except RuntimeError:  # pragma: no cover - the loop is already closed
            return False
        return False

    def _maybe_wake() -> None:
        # Runs on the loop (hopped from the job watcher thread).
        _request_wake("[notice] 后台任务的结果到了——查看并继续。")

    def _notice_arrived(_text: str) -> None:
        """From the job watcher thread: hop onto the loop and consider a wake."""
        try:
            service._loop.call_soon_threadsafe(_maybe_wake)
        except RuntimeError:  # pragma: no cover - the loop is already closed
            pass

    if world.notices is not None:
        world.notices.on_put = _notice_arrived

    return {
        "initialize": initialize,
        "session.create": session_create,
        "session.list": session_list,
        "session.load": session_load,
        "session.prompt": session_prompt,
        "session.resume": session_resume,
        "session.interrupt": session_interrupt,
        "session.steer": session_steer,
        "session.command": session_command,
        "session.delete": session_delete,
        "model.set": model_set,
        "model.list": model_list,
        "model.providers": model_providers,
        "model.connect": model_connect,
        "permissions.state": permissions_state,
        "permissions.revoke": permissions_revoke,
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
        "session.inbox": session_inbox,
        "session.jobs": session_jobs,
        "session.stats": session_stats,
        "session.agents": session_agents,
        "session.agent.select": session_agent_select,
        "mcp.list": mcp_list,
        "mcp.connect": mcp_connect,
        "mcp.login": mcp_login,
        "mcp.logout": mcp_logout,
        "mcp.add": mcp_add,
        "shutdown": shutdown_handler,
    }
