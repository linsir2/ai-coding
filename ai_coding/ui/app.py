"""主 Application：组装 layout + 事件绑定。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D

from ai_coding.ui.body import BodyBuffer
from ai_coding.ui.footer import render_footer
from ai_coding.ui.header import render_header
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
    from ai_coding.security.approval import CLIApprovalCallback, CLIUserPromptCallback
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

    loop = _build_loop(
        engine, sessions, registry, app_cfg, workspace, skills,
        approval=CLIApprovalCallback(),
        observer=None,
        trace=recorder,
        guards=_make_guards(app_cfg),
        hooks=_make_hooks(app_cfg, workspace),
        steering=steering,
    )


    body = BodyBuffer()
    palette = CommandPalette(PALETTE_ENTRIES)
    session = sessions.create()
    turn_start = time.monotonic()

    body.write("  ✨ Welcome to Nexus! ✨\n")
    body.write("  Describe a task, or use / for commands.\n\n")

    header_control = FormattedTextControl(lambda: render_header(loop))
    footer_control = FormattedTextControl(
        lambda: render_footer(loop, turn_start)
    )
    body_control = BufferControl(buffer=Buffer())
    palette_control = FormattedTextControl(
        lambda: palette.render() if palette.is_open else []
    )

    kb = KeyBindings()

    @kb.add("/")
    def _toggle_palette(event: Any) -> None:
        nonlocal session
        if palette.is_open:
            palette.close()
            event.app.layout.focus(body_control)
        else:
            palette.open()
            event.app.layout.focus(palette_control)

    @kb.add("escape")
    def _close_palette(event: Any) -> None:
        nonlocal session
        if palette.is_open:
            palette.close()
            event.app.layout.focus(body_control)

    @kb.add("down")
    def _palette_down(event: Any) -> None:
        if palette.is_open:
            palette.handle_key("down")

    @kb.add("up")
    def _palette_up(event: Any) -> None:
        if palette.is_open:
            palette.handle_key("up")

    @kb.add("enter")
    def _palette_select(event: Any) -> None:
        nonlocal session
        if palette.is_open:
            entry = palette.select()
            event.app.layout.focus(body_control)
            asyncio.create_task(_run_command(entry.name))

    async def _run_command(name: str) -> None:
        nonlocal session
        from ai_coding.ui.console import DialogueConsole
        from ai_coding.ui.repl import handle_input

        console = DialogueConsole()
        action, new_session = await handle_input(
            loop, f"/{name}", session, sessions, console
        )
        if action.name == "new_session":
            session = new_session

    @kb.add("c-q")
    def _exit(event: Any) -> None:
        event.app.exit()

    palette_window = Window(
        content=palette_control,
        width=D(preferred=60),
        height=D(preferred=14),
    )

    root = FloatContainer(
        content=HSplit([
            Window(
                content=header_control,
                height=D(preferred=12),
                style="class:header",
            ),
            Window(content=body_control, wrap_lines=False),
            Window(content=footer_control, height=1, style="class:footer"),
        ]),
        floats=[
            Float(
                content=palette_window,
                top=4,
                left=10,
                transparent=True,
            ),
        ],
    )

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
