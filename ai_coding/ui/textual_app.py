"""The Textual application: shell + loop wiring."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from textual import work
from textual.app import App, ComposeResult, get_system_commands_provider
from textual.binding import Binding
from textual.command import CommandPalette
from textual.widgets import Input
from textual.worker import Worker, WorkerState

from ai_coding.ui.blocks import Block, TranscriptObserver, full_call_view
from ai_coding.ui.commands import SlashCommandProvider
from ai_coding.ui.input_bar import InputBar
from ai_coding.ui.modals import ALWAYS_EXCLUDED, ApprovalScreen, AskScreen
from ai_coding.ui.repl import REPLAction, handle_input
from ai_coding.ui.resume_screen import ResumeScreen
from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.transcript import Transcript

_FLUSH_SECONDS = 0.05


class AICodingApp(App[None]):
    """Full-screen coding assistant UI."""

    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+y", "copy_last_tool", "Copy last tool output", show=False),
        Binding("escape", "cancel_turn", "Cancel tool batch", show=False),
    ]
    COMMANDS = {SlashCommandProvider, get_system_commands_provider}

    # Declared here (not only assigned in on_mount) so mypy strict can infer them.
    # ``_agent_loop``, not ``_loop``: Textual's App owns ``self._loop`` (its
    # asyncio loop) and overwrites it in ``run_async``.
    _agent_loop: Any
    _session: Any
    _sessions: Any
    _workspace: str
    _model_name: str
    _status: StatusBar
    _transcript: Transcript
    _input: Input
    _observer: TranscriptObserver
    _cancel_event: asyncio.Event

    def __init__(
        self,
        *,
        loop: Any,
        session: Any,
        sessions: Any,
        workspace: str,
        model_name: str = "",
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self._agent_loop = loop
        self._session = session
        self._sessions = sessions
        self._workspace = workspace
        self._model_name = model_name
        self._observer = TranscriptObserver(self._append_block)
        # R8c's switch, finally connected: ``ToolDispatcher`` has held this event
        # since it was written (``dispatch.py:170-171``, "the user's 'drop this
        # batch' switch (Esc)"), and ``run_with_deadline`` abandons the tool and
        # raises ``RunCancelled`` when it fires (``deadline.py:132``). Nothing
        # constructed it, so a long tool call had no way out but quitting.
        #
        # ``run_app`` passes the same instance it handed to the dispatcher; the
        # default keeps the app self-contained for tests and for any future
        # caller that does not wire a loop.
        self._cancel_event = cancel_event if cancel_event is not None else asyncio.Event()
        # Lines submitted while a turn is running.  The old guard against two
        # concurrent ``process_input`` calls was ``input.disabled = True`` for
        # the whole turn — correct but it made the box unusable.  The guard is
        # the same (never two turns at once), it just queues the input instead
        # of dropping it.  ``_turn_active`` is the synchronous flag the submit
        # path reads; ``on_worker_state_changed`` clears it and drains the queue.
        self._queued: deque[str] = deque()
        self._turn_active: bool = False
        # Tools the operator chose "Always" for this session.  In-memory only:
        # a persisted grant would outlive the decision that made it.
        # ``bash`` is never admitted — an always-allow on bash approves every
        # command, which is the one grant that must not exist.
        self._always_allow: set[str] = set()

    # ---- shell -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(self._workspace, self._model_name)
        yield Transcript()
        yield InputBar()

    def on_mount(self) -> None:
        self._status = self.query_one(StatusBar)
        self._transcript = self.query_one(Transcript)
        self._input = self.query_one("#prompt", Input)
        # VerticalScroll is focusable, so Textual's auto-focus lands on the
        # transcript; the prompt must own the keyboard for Enter to submit.
        self._input.focus()
        self.set_interval(_FLUSH_SECONDS, self._flush_stream)

    # ---- command palette ---------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.value == "/" and not CommandPalette.is_open(cast("App[object]", self)):
            self.action_command_palette()

    def on_command_palette_closed(self, event: CommandPalette.Closed) -> None:
        if self._input.value == "/":
            self._input.value = ""

    # ---- streaming (sync sink → buffered flush) ---------------------------

    def _on_delta(self, text: str) -> None:
        self._transcript.stream(text)

    def _flush_stream(self) -> None:
        self._transcript.flush()

    def _append_block(self, block: Block) -> None:
        self._transcript.append_block(block)

    # ---- turn ------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self._input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self.submit_slash(text)
        else:
            self._transcript.append_user(text, _clock())
            self._submit_text(text)

    def submit_slash(self, text: str) -> None:
        """Record a slash command as a user turn, then run it.

        Both the input box (typed + Enter) and the command palette
        (selected) go through here so the history is complete.
        """
        self._transcript.append_user(text, _clock())
        self._submit_text(text)

    def _submit_text(self, text: str) -> None:
        """Run it now, or queue it behind the turn that is already running."""
        if self._turn_active:
            self._enqueue(text)
        else:
            self._dispatch(text)

    def _enqueue(self, text: str) -> None:
        # Bare /resume opens a modal picker and returns; its worker cannot
        # stand in for the whole resume, so it is refused rather than queued
        # (a background turn could then race the picker for the exclusive
        # group).  Every other submission — text, /plan, /clear — queues.
        if text == "/resume" or text.startswith("/resume "):
            self._transcript.append_block(
                Block("info", "[queue] /resume 请在回合结束后使用")
            )
            return
        self._queued.append(text)
        self._transcript.append_block(Block("info", "[queue] 回合结束后执行"))

    def _dispatch(self, text: str) -> None:
        """Start the turn worker; its completion is drained by the state hook."""
        self._turn_active = True
        if text.startswith("/"):
            self.run_command(text)
        else:
            self.run_turn(text)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """A turn worker reached a terminal state: free the slot, drain the queue.

        One hook rather than a done callback per worker: Textual's ``Worker``
        exposes no public done-callback, and every turn worker (text, command,
        resume) is in the ``turn`` group on this node, so they all land here.
        """
        if event.worker.group != "turn" or event.worker.node is not self:
            return
        if event.state not in (WorkerState.SUCCESS, WorkerState.CANCELLED, WorkerState.ERROR):
            return
        self._turn_done()

    def _turn_done(self) -> None:
        self._turn_active = False
        if not self._queued:
            return
        nxt = self._queued.popleft()
        # Deferred, not called inline: starting a new exclusive worker cancels
        # every worker in the group, including this one still unwinding its
        # finally (which would interrupt ``end_assistant``).  ``call_later``
        # runs after this worker is removed.
        self.call_later(lambda: self._dispatch(nxt))

    @work(exclusive=True, group="turn", thread=False)
    async def run_command(self, text: str) -> None:
        from ai_coding.ui.console import TextualConsole

        self._status.set_busy(True)
        try:
            if text == "/plan" or text.startswith("/plan "):
                await self._plan_turn(_plan_topic(text))
                return
            if text == "/resume":
                # Bare /resume is a picker, not a printout: the saved list is too
                # long to read and its IDs cannot be typed back.
                if self._saved_session_count() == 0:
                    self._transcript.append_block(
                        Block("info", "[resume] no saved sessions")
                    )
                    return
                self._open_resume_picker()
                return
            console = TextualConsole(self._transcript)
            action, new_session = await handle_input(
                self._agent_loop, text, self._session, self._sessions, console
            )
            if action is REPLAction.NEW_SESSION:
                await self._switch_session(new_session)
            elif action is REPLAction.QUIT:
                self.exit()
        finally:
            self._ensure_approval_provider()
            model = getattr(getattr(self._agent_loop, "engine", None), "model_config", None)
            self._status.set_model(getattr(model, "name", "") or "")
            self._status.set_busy(False)
            self._input.focus()

    def _saved_session_count(self) -> int:
        """How many sessions the picker would offer (excluding this one)."""
        current = getattr(self._session, "session_id", None)
        return sum(
            1
            for s in self._sessions.list_sessions()
            if current is None or s.session_id != current
        )

    def _open_resume_picker(self) -> None:
        """Push the session picker; a choice resumes with history replayed.

        Fire-and-forget on purpose: awaiting the screen here would park the
        ``run_command`` worker on it, and a cancel/quit while it waits lands
        the ``finally`` below on an already-torn-down DOM (``NoMatches``).
        The dismiss callback starts its own worker instead.
        """
        current = getattr(self._session, "session_id", None)
        self.push_screen(
            ResumeScreen(self._sessions.list_sessions(), exclude_id=current),
            self._resume_chosen,
        )

    def _resume_chosen(self, chosen: str | None) -> None:
        """A picker dismissal: nothing on cancel, a worker on a choice."""
        if chosen:
            self._turn_active = True
            self._finish_resume(chosen)

    @work(exclusive=True, group="turn", thread=False)
    async def _finish_resume(self, chosen: str) -> None:
        self._status.set_busy(True)
        try:
            matches = self._sessions.resolve(chosen)
            if len(matches) != 1:
                self._transcript.append_block(
                    Block("info", f"[resume] session '{chosen[:12]}' not found")
                )
                return
            full = self._sessions.load(matches[0].session_id) or matches[0]
            await self._switch_session(full)
        finally:
            self._status.set_busy(False)
            self._input.focus()

    async def _switch_session(self, new_session: Any) -> None:
        """Point the app at a new session and make its history visible.

        Switching data without rebuilding the view was the old bug: the
        transcript kept showing the previous conversation while the loop
        talked to the new one.  ``/clear`` lands here too (empty messages =
        a clean transcript, which is what "fresh session" means).
        """
        self._session = new_session
        await self._transcript.replay_history(
            getattr(new_session, "messages", ()),
            getattr(new_session, "step_deliveries", ()),
        )

    async def _plan_turn(self, topic: str | None = None) -> None:
        """/plan plans the topic: from the command line, or asked first.

        The workflow itself lives in ``plan_runner.plan_and_run`` — it used to
        be written out here *and* in the REPL dispatcher, and the two drifted
        because only this copy was reachable in the shipped app (the router
        intercepts ``/plan`` before the generic handler sees it).
        """
        from ai_coding.core.plan_runner import plan_and_run
        from ai_coding.ui.console import TextualConsole

        async def ask() -> str | None:
            screen = AskScreen("What should I plan?")
            try:
                return await self.push_screen_wait(screen)
            finally:
                if self.screen is screen:
                    self.pop_screen()

        self._transcript.begin_assistant(_clock())
        try:
            await plan_and_run(
                self._agent_loop, self._session, TextualConsole(self._transcript),
                topic=topic, ask_topic=ask,
                on_delta=self._on_delta, observer=self._observer,
            )
        finally:
            # WP3: backfill source; getattr because test stubs pass
            # session=object() (no messages → no backfill, previews stay).
            await self._transcript.end_assistant(getattr(self._session, "messages", ()))

    def _ensure_approval_provider(self, dispatcher: Any | None = None) -> None:
        """4.1: ``/permissions confirm`` builds a fresh callback with no provider.

        Called after every slash command; ``dispatcher`` is injectable so the
        regression test can pass a throwaway one instead of a real loop.
        """
        from ai_coding.security.approval import CLIApprovalCallback

        if dispatcher is None:
            dispatcher = getattr(self._agent_loop, "dispatcher", None)
        callback = getattr(dispatcher, "approval", None) if dispatcher else None
        if isinstance(callback, CLIApprovalCallback):
            callback.set_provider(self.approval_provider)

    @work(exclusive=True, group="turn", thread=False)
    async def run_turn(self, text: str) -> None:
        self._status.set_busy(True)
        try:
            self._transcript.begin_assistant(_clock())
            await self._agent_loop.process_input(
                self._session, text, on_delta=self._on_delta, observer=self._observer
            )
        finally:
            # WP3: backfill source; getattr because test stubs pass
            # session=object() (no messages → no backfill, previews stay).
            await self._transcript.end_assistant(getattr(self._session, "messages", ()))
            self._status.set_busy(False)
            self._input.focus()

    def action_cancel_turn(self) -> None:
        """Abort the running turn (Esc).

        Two layers, and both are needed.  ``_cancel_event`` is the tool
        layer's fast path (a batch in flight comes back ``cancelled_by_user``
        and the model sees it).  But a turn is usually *not* in a tool batch
        — it is inside a model call, which never watches that event — so Esc
        used to do nothing visible while tokens burned.  Cancelling the
        "turn"-group worker delivers ``CancelledError`` wherever the turn
        happens to be; the loop already closes that as ABORTED, persists the
        partial work, and re-raises, and ``run_turn``'s finally closes the
        transcript and restores focus.  Workers already being cancelled are
        skipped: a second Esc mid-unwind would interrupt that finally and
        leave the turn half-closed (busy flag set, transcript turn open).
        """
        self._cancel_event.set()
        for worker in self.workers:
            if worker.group != "turn" or worker.node is not self:
                continue
            if worker.is_cancelled:
                continue
            worker.cancel()

    def action_copy_last_tool(self) -> None:
        """Copy the most recent finished tool call to the system clipboard.

        Resolved through ``full_call_view`` against the session — the same
        source the settled cards render — so what lands in the clipboard is
        the full params + output, not a preview.  Textual delivers it via
        OSC 52 (no macOS Terminal); in headless/test drivers without a driver
        only the in-app clipboard is set.
        """
        call_id = self._transcript.last_finished
        if call_id is None:
            self._transcript.append_block(Block("info", "没有可复制的工具输出"))
            return
        view = full_call_view(getattr(self._session, "messages", ()), call_id)
        parts = [p for p in (view.params, view.output) if p is not None]
        if not parts:
            self._transcript.append_block(Block("info", "没有可复制的工具输出"))
            return
        text = "\n\n".join(parts)
        self.copy_to_clipboard(text)
        self._transcript.append_block(Block("info", f"已复制 {len(text)} 字符"))

    # ---- human channels --------------------------------------------------

    async def approval_provider(
        self, tool_name: str, params: dict[str, Any], reason: str
    ) -> str:
        """Ask through a modal; return the string ``approval.py`` parses.

        The gate hands over the structured call, so the modal can render the
        parameters as they are.  It used to arrive as one truncated prompt
        string that the UI had to re-parse, and the 120-char cut meant anything
        longer — a real ``bash`` command, say — was simply not shown.
        """
        # "Always allow" (session-scoped, bash excluded): answer without a modal.
        if tool_name not in ALWAYS_EXCLUDED and tool_name in self._always_allow:
            return "y"

        screen = ApprovalScreen(tool_name=tool_name, params=params, reason=reason)

        try:
            # The annotation, not a ``str()`` wrapper: it is what binds
            # ``ScreenResultType`` to ``str`` (an unconstrained call resolves to
            # ``Screen[object]`` under strict mypy).
            answer: str = await self.push_screen_wait(screen)
        finally:
            # push_screen_wait is shielded: a cancelled turn does not close the
            # screen (spec §3.3-5), so close it here.
            if self.screen is screen:
                self.pop_screen()

        if answer == "a":
            # ``a`` means allow now; whether it also grants is ALWAYS_EXCLUDED's
            # question, and the record must never be the deciding factor for the
            # allow itself — returning the raw "a" would reach approval.py's
            # parser, which fails closed to NO.
            if tool_name not in ALWAYS_EXCLUDED:
                self._always_allow.add(tool_name)
            return "y"
        return answer

    async def ask_provider(self, prompt: str, options: Sequence[str] = ()) -> str:
        """Ask through a modal; return what the human typed (``""`` means no answer).

        ``options`` arrives from the tool (see ``ask_user_tool.py``) and used to
        be flattened into ``prompt`` one level down, which left
        ``AskScreen``'s own ``options`` parameter permanently empty.  It is
        forwarded now, so the modal can list them.
        """
        screen = AskScreen(prompt, tuple(options))
        try:
            answer: str = await self.push_screen_wait(screen)
            return answer
        finally:
            if self.screen is screen:
                self.pop_screen()


def _clock() -> str:
    return datetime.now().strftime("%H:%M")


def _plan_topic(text: str) -> str | None:
    """The topic in ``/plan <topic>``, or ``None`` for a bare ``/plan``.

    R1-5b: the topic used to be dropped on the floor — "/plan 改一下 loader"
    ran the fixed topic-less prompt and never said so.  Slicing requires a
    real space, so any token that merely starts with the same letters is left
    alone and takes the unknown-command path rather than losing its tail.
    """
    stripped = text.strip()
    if not stripped.startswith("/plan "):
        return None
    return stripped[len("/plan"):].strip() or None


async def run_app(path: str | None, workspace: str, skills: str | None) -> None:
    """Build the world and run the Textual app (signature kept from cli.py)."""
    from ai_coding.cli import (
        _build_loop,
        _build_tool_registry,
        _make_hooks,
        _make_recorder,
    )
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

    approval = CLIApprovalCallback()
    # Built before the app so the loop and the app can share one event: the app
    # owns the Esc binding and sets it, the dispatcher waits on it.
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

    model = getattr(getattr(loop, "engine", None), "model_config", None)
    model_name = getattr(model, "name", "") or ""

    app = AICodingApp(
        loop=loop, session=sessions.create(), sessions=sessions,
        workspace=workspace, model_name=model_name,
        cancel_event=cancel,
    )
    approval.set_provider(app.approval_provider)
    user_prompt.set_provider(app.ask_provider)

    try:
        await app.run_async()
    finally:
        await loop.aclose()
        await mcp_manager.aclose()
