"""The Textual application: shell + loop wiring."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input

from ai_coding.ui.blocks import Block, TranscriptObserver
from ai_coding.ui.input_bar import InputBar
from ai_coding.ui.modals import ApprovalScreen, AskScreen
from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.transcript import Transcript

_FLUSH_SECONDS = 0.05


class AICodingApp(App[None]):
    """Full-screen coding assistant UI."""

    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

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
    _turn_started: float

    def __init__(
        self,
        *,
        loop: Any,
        session: Any,
        sessions: Any,
        workspace: str,
        model_name: str = "",
    ) -> None:
        super().__init__()
        self._agent_loop = loop
        self._session = session
        self._sessions = sessions
        self._workspace = workspace
        self._model_name = model_name
        self._observer = TranscriptObserver(self._append_block)
        self._turn_started = time.monotonic()

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
        if not text or self._input.disabled:
            return
        self._input.value = ""
        self._transcript.append_user(text, _clock())
        self.run_turn(text)

    @work(exclusive=True, group="turn", thread=False)
    async def run_turn(self, text: str) -> None:
        self._input.disabled = True
        self._status.set_busy(True)
        self._turn_started = time.monotonic()
        try:
            self._transcript.begin_assistant(_clock())
            await self._agent_loop.process_input(
                self._session, text, on_delta=self._on_delta, observer=self._observer
            )
        finally:
            await self._transcript.end_assistant()
            self._input.disabled = False
            self._status.set_busy(False)
            self._input.focus()

    # ---- human channels --------------------------------------------------

    async def approval_provider(self, prompt: str) -> str:
        """Ask through a modal; return the string ``approval.py`` parses.

        ``prompt`` is the whole formatted text (spec §4.2: the provider
        interface flattens tool name + reason + params), so it is shown as the
        body and the tool name comes from its first line.
        """
        screen = ApprovalScreen(tool_name=_first_line(prompt), params={}, reason=prompt)
        try:
            # The annotation, not a ``str()`` wrapper: it is what binds
            # ``ScreenResultType`` to ``str`` (an unconstrained call resolves to
            # ``Screen[object]`` under strict mypy).
            answer: str = await self.push_screen_wait(screen)
            return answer
        finally:
            # push_screen_wait is shielded: a cancelled turn does not close the
            # screen (spec §3.3-5), so close it here.
            if self.screen is screen:
                self.pop_screen()

    async def ask_provider(self, prompt: str) -> str:
        screen = AskScreen(prompt)
        try:
            answer: str = await self.push_screen_wait(screen)
            return answer
        finally:
            if self.screen is screen:
                self.pop_screen()


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else "?"


def _clock() -> str:
    return datetime.now().strftime("%H:%M")


async def run_app(path: str | None, workspace: str, skills: str | None) -> None:
    """Build the world and run the Textual app (signature kept from cli.py)."""
    from ai_coding.cli import (
        _build_loop,
        _build_tool_registry,
        _make_guards,
        _make_hooks,
        _make_recorder,
    )
    from ai_coding.config.loader import ConfigManager
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
    loop = _build_loop(
        engine, sessions, registry, app_cfg, workspace, skills,
        approval=approval,
        observer=None,
        trace=recorder,
        guards=_make_guards(app_cfg),
        hooks=_make_hooks(app_cfg, workspace),
        steering=steering,
    )

    model = getattr(getattr(loop, "engine", None), "model_config", None)
    model_name = getattr(model, "name", "") or ""

    app = AICodingApp(
        loop=loop, session=sessions.create(), sessions=sessions,
        workspace=workspace, model_name=model_name,
    )
    approval.set_provider(app.approval_provider)
    user_prompt.set_provider(app.ask_provider)

    try:
        await app.run_async()
    finally:
        await loop.aclose()
        await mcp_manager.aclose()
