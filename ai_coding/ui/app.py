"""主 Application：组装 layout + 事件绑定。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D

from ai_coding.ui.body import BodyBuffer
from ai_coding.ui.footer import render_footer
from ai_coding.ui.header import render_header
from ai_coding.ui.observer import BodyObserver
from ai_coding.ui.palette import CommandPalette, PaletteEntry
from ai_coding.ui.theme import STYLE

PALETTE_ENTRIES = [
    PaletteEntry("plan", "Create and refine an implementation plan", "📝"),
    PaletteEntry("review", "Review current changes and surface issues", "🔍"),
    PaletteEntry("model", "Change model and reasoning effort", "🤖"),
    PaletteEntry("status", "Show session and runtime status", "📊"),
    PaletteEntry("compact", "Summarize conversation to reduce context", "📄"),
    PaletteEntry("resume", "Resume a saved session", "⏰"),
    PaletteEntry("permissions", "Manage approval policy", "🛡"),
    PaletteEntry("todo", "Show current task list", "✓"),
    PaletteEntry("skills", "List available skills", "📦"),
    PaletteEntry("config", "Show effective configuration", "⚙"),
    PaletteEntry("memory", "Show memory status", "🧠"),
    PaletteEntry("budget", "Show session token totals", "📈"),
    PaletteEntry("help", "Show available commands", "❓"),
]

HEADER_HEIGHT = 9
PALETTE_WIDTH = 72
PALETTE_HEIGHT = len(PALETTE_ENTRIES) + 3
INPUT_HEIGHT = 3


async def run_app(path: str | None, workspace: str, skills: str | None) -> None:
    """组装并运行全屏 TUI。"""
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

    body = BodyBuffer()
    body_observer = BodyObserver(body)
    approval = CLIApprovalCallback()

    loop = _build_loop(
        engine, sessions, registry, app_cfg, workspace, skills,
        approval=approval,
        observer=body_observer,
        trace=recorder,
        guards=_make_guards(app_cfg),
        hooks=_make_hooks(app_cfg, workspace),
        steering=steering,
    )

    palette = CommandPalette(PALETTE_ENTRIES)
    session = sessions.create()
    turn_start = time.monotonic()
    input_buffer = Buffer()
    input_event = asyncio.Event()
    awaiting_input = False

    body.write("  ✨ Welcome to ai-coding! ✨\n")
    body.write("  Describe a task, or use / for commands.\n\n")

    header_control = FormattedTextControl(lambda: render_header(loop))
    footer_control = FormattedTextControl(
        lambda: render_footer(loop, turn_start)
    )
    body_control = FormattedTextControl(lambda: body.get_formatted_text())
    input_control = BufferControl(buffer=input_buffer)
    palette_control = FormattedTextControl(
        lambda: palette.render() if palette.is_open else []
    )

    body_window = Window(content=body_control, wrap_lines=False)
    input_window = Window(
        content=input_control,
        height=D(preferred=INPUT_HEIGHT),
        style="class:input",
    )
    palette_window = Window(
        content=palette_control,
        width=D(preferred=PALETTE_WIDTH),
        height=D(preferred=PALETTE_HEIGHT) if palette.is_open else D(preferred=0),
        wrap_lines=False,
    )

    kb = KeyBindings()

    @kb.add("/")
    def _toggle_palette(event: Any) -> None:
        if palette.is_open:
            palette.close()
            event.app.invalidate()
        else:
            palette.open()
            event.app.invalidate()

    @kb.add("backspace", filter=Condition(lambda: palette.is_open))
    def _backspace_palette(event: Any) -> None:
        palette.close()
        event.app.invalidate()

    @kb.add("escape")
    def _close_palette(event: Any) -> None:
        if palette.is_open:
            palette.close()
            event.app.invalidate()

    @kb.add("down")
    def _palette_down(event: Any) -> None:
        if palette.is_open:
            palette.handle_key("down")
            event.app.invalidate()

    @kb.add("up")
    def _palette_up(event: Any) -> None:
        if palette.is_open:
            palette.handle_key("up")
            event.app.invalidate()

    async def _input_provider(prompt: str) -> str:
        nonlocal awaiting_input
        body.write(f"\n{prompt}\n")
        body.write("  (type answer, Enter to submit)\n")
        input_buffer.reset()
        awaiting_input = True
        input_event.clear()
        try:
            await input_event.wait()
            return input_buffer.text
        finally:
            awaiting_input = False

    user_prompt.set_provider(_input_provider)
    approval.set_provider(_input_provider)

    @kb.add("enter")
    def _enter(event: Any) -> None:
        if palette.is_open:
            entry = palette.select()
            event.app.invalidate()
            asyncio.create_task(_run_command(entry.name))
        elif awaiting_input:
            input_event.set()
        else:
            text = input_buffer.text
            if text.strip():
                body.write(f"> {text}\n")
                input_buffer.reset()
                asyncio.create_task(_submit_text(text))

    async def _run_command(name: str) -> None:
        nonlocal session, turn_start
        turn_start = time.monotonic()
        from ai_coding.ui.console import DialogueConsole
        from ai_coding.ui.repl import handle_input

        console = DialogueConsole()
        action, new_session = await handle_input(
            loop, f"/{name}", session, sessions, console
        )
        if action.name == "new_session":
            session = new_session

    async def _submit_text(text: str) -> None:
        nonlocal turn_start
        turn_start = time.monotonic()
        await loop.process_input(session, text, on_delta=body.write)

    @kb.add("c-c")
    def _exit(event: Any) -> None:
        event.app.exit()

    root = HSplit([
        Window(
            content=header_control,
            height=D(preferred=HEADER_HEIGHT),
            style="class:header",
        ),
        body_window,
        palette_window,
        input_window,
        Window(content=footer_control, height=1, style="class:footer"),
    ])

    app = Application(
        layout=Layout(root),
        key_bindings=kb,
        style=STYLE,
        full_screen=True,
    )

    try:
        await app.run_async()
    finally:
        await loop.aclose()
        await mcp_manager.aclose()
