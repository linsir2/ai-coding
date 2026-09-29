"""Scrolling conversation transcript: message blocks + streaming markdown."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widget import AwaitMount
from textual.widgets import Markdown, Static

_FLUSH_CHARS = 4096


class Transcript(VerticalScroll):
    """Append-only conversation area with a "stay at the bottom" policy.

    Streaming is two-phase because ``on_delta`` is a *sync* callback while
    ``Markdown.append`` is optionally awaitable: ``stream`` only buffers,
    ``flush`` (driven by a 50ms timer in the app) renders, and the async
    ``end_assistant`` awaits the final write (spec §3.3-1).
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._pending = ""
        self._markdown: Markdown | None = None
        self._mount: AwaitMount | None = None

    def append_user(self, text: str, timestamp: str) -> None:
        self.mount(Static(_header("◯", "You", timestamp), classes="msg-header"))
        self.mount(Static(text, classes="user-bubble"))
        self._follow()

    def begin_assistant(self, timestamp: str) -> None:
        self.mount(Static(_header("✦", "Nexus", timestamp), classes="msg-header"))
        self._markdown = Markdown()
        self._mount = self.mount(self._markdown)

    def stream(self, text: str) -> None:
        self._pending += text
        if len(self._pending) >= _FLUSH_CHARS:
            self.flush()

    def flush(self) -> None:
        if self._pending and self._markdown is not None and self._markdown.is_mounted:
            self._markdown.append(self._pending)  # AwaitComplete: starts now
            self._pending = ""
        self._follow()

    async def end_assistant(self) -> None:
        pending = self._pending
        markdown = self._markdown
        mount = self._mount
        self._pending = ""
        self._markdown = None
        self._mount = None
        if pending and markdown is not None:
            if mount is not None:
                # Markdown's on_mount starts with update(""), which would wipe
                # anything appended before the Mount message is processed.
                await mount
            await markdown.append(pending)
        self._follow()

    def append_block(self, block: Any) -> None:
        # ``Any`` until Task 3 lands ``Block``; importing it now would be a
        # cycle (blocks must stay Textual-free, so it cannot import Transcript
        # back).  Task 3 tightens this annotation.
        self.mount(Static(block.render(), classes=f"block-{block.kind}"))
        self._follow()

    def clear(self) -> None:
        for child in list(self.children):
            child.remove()
        self._pending = ""
        self._markdown = None
        self._mount = None

    def _follow(self) -> None:
        """Scroll to the newest content only if the user is already at the end."""
        if self.is_vertical_scroll_end:
            self.scroll_end(animate=False)


def _header(icon: str, name: str, timestamp: str) -> Text:
    text = Text()
    text.append(f"{icon} ", style="bold blue")
    text.append(name, style="bold")
    text.append(f"  {timestamp}", style="dim")
    return text
