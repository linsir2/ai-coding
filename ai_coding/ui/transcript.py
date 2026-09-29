"""Scrolling conversation transcript: message blocks + streaming markdown."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widget import AwaitMount
from textual.widgets import Collapsible, Markdown, RichLog, Static

from ai_coding.ui.blocks import Block

_FLUSH_CHARS = 4096


class Turn(Vertical):
    """One assistant turn: a header, streaming markdown, and collapsible tool blocks.

    Text and tools interleave: each ``start_tool`` finalizes the current markdown
    block so subsequent text starts a fresh one.  A tool is a collapsed
    ``Collapsible`` whose ``RichLog`` body receives the params preview and any
    streamed chunks; ``end_tool`` rewrites the title with a ✓/✗ mark.
    """

    def __init__(self, timestamp: str) -> None:
        super().__init__()
        self._timestamp = timestamp
        self._pending = ""
        self._markdown: Markdown | None = None
        self._mount: AwaitMount | None = None
        self._stale: list[tuple[Markdown, AwaitMount | None, str]] = []
        self._tools: dict[str, tuple[Collapsible, RichLog]] = {}
        self._header_mounted = False

    def ensure_header(self) -> None:
        # Mounted eagerly rather than in compose(): compose runs one message-loop
        # tick after attach, so a stream() in the same tick would put its
        # Markdown above the header.
        if not self._header_mounted:
            self.mount(Static(_header("✦", "Nexus", self._timestamp), classes="msg-header"))
            self._header_mounted = True

    def stream(self, text: str) -> None:
        self.ensure_header()
        if self._markdown is None:
            self._markdown = Markdown()
            self._mount = self.mount(self._markdown)
        self._pending += text
        if len(self._pending) >= _FLUSH_CHARS:
            self.flush()

    def flush(self) -> None:
        if self._pending and self._markdown is not None and self._markdown.is_mounted:
            self._markdown.append(self._pending)  # AwaitComplete: starts now
            self._pending = ""

    def start_tool(self, call_id: str, title: str, body: str) -> None:
        self.flush()
        if self._pending:
            # The markdown is not mounted yet, so its text could not be flushed;
            # carry it so ``end`` still renders it, before this tool.
            assert self._markdown is not None  # pending implies a markdown exists
            self._stale.append((self._markdown, self._mount, self._pending))
            self._pending = ""
        self._markdown = None
        self._mount = None
        log = RichLog()
        log.write(body)
        collapsible = Collapsible(log, title=title, collapsed=True)
        self._tools[call_id] = (collapsible, log)
        self.mount(collapsible)

    def tool_chunk(self, call_id: str, text: str) -> None:
        tool = self._tools.get(call_id)
        if tool is not None:
            tool[1].write(text)

    def end_tool(self, call_id: str, title: str) -> None:
        tool = self._tools.get(call_id)
        if tool is not None:
            tool[0].title = title

    async def end(self) -> None:
        for markdown, mount, pending in self._stale:
            if mount is not None:
                # Markdown's on_mount starts with update(""), which would wipe
                # anything appended before the Mount message is processed.
                await mount
            await markdown.append(pending)
        self._stale.clear()
        if self._pending and self._markdown is not None:
            if self._mount is not None:
                await self._mount
            await self._markdown.append(self._pending)
            self._pending = ""
        self._tools.clear()


class Transcript(VerticalScroll):
    """Append-only conversation area with a "stay at the bottom" policy.

    Streaming is two-phase because ``on_delta`` is a *sync* callback while
    ``Markdown.append`` is optionally awaitable: ``stream`` only buffers,
    ``flush`` (driven by a 50ms timer in the app) renders, and the async
    ``end_assistant`` awaits the final write (spec §3.3-1).
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._turn: Turn | None = None

    def append_user(self, text: str, timestamp: str) -> None:
        self.mount(Static(_header("◯", "You", timestamp), classes="msg-header"))
        self.mount(Static(text, classes="user-bubble"))
        self._follow()

    def begin_assistant(self, timestamp: str) -> None:
        self._turn = Turn(timestamp)
        self.mount(self._turn)
        self._turn.ensure_header()
        self._follow()

    def stream(self, text: str) -> None:
        if self._turn is None:
            self._turn = Turn(_clock())
            self.mount(self._turn)
        self._turn.stream(text)
        self._follow()

    def flush(self) -> None:
        if self._turn is not None:
            self._turn.flush()
        self._follow()

    async def end_assistant(self) -> None:
        turn = self._turn
        self._turn = None
        if turn is not None:
            await turn.end()
        self._follow()

    def append_block(self, block: Block) -> None:
        turn = self._turn
        if turn is not None and block.kind == "tool":
            if block.tool_status == "running":
                turn.start_tool(block.call_id, _tool_title("▸", block), block.text)
            elif block.tool_status in {"ok", "failed"}:
                mark = "✓" if block.tool_status == "ok" else "✗"
                turn.end_tool(block.call_id, _tool_title(mark, block))
            else:
                self.mount(Static(block.render(), classes=f"block-{block.kind}"))
        elif turn is not None and block.kind == "tool-chunk":
            turn.tool_chunk(block.call_id, block.text)
        else:
            self.mount(Static(block.render(), classes=f"block-{block.kind}"))
        self._follow()

    def clear(self) -> None:
        for child in list(self.children):
            child.remove()
        self._turn = None

    def _follow(self) -> None:
        """Scroll to the newest content only if the user is already at the end."""
        if self.is_vertical_scroll_end:
            self.scroll_end(animate=False)


def _tool_title(mark: str, block: Block) -> str:
    title = f"{mark} {block.tool_name}"
    if block.tool_target:
        title += f"  {block.tool_target}"
    return title


def _clock() -> str:
    return datetime.now().strftime("%H:%M")


def _header(icon: str, name: str, timestamp: str) -> Text:
    text = Text()
    text.append(f"{icon} ", style="bold blue")
    text.append(name, style="bold")
    text.append(f"  {timestamp}", style="dim")
    return text
