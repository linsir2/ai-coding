"""Slash-command provider for the Textual command palette."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, cast

from textual.command import DiscoveryHit, Hit, Provider

from ai_coding.ui.plain import plain

if TYPE_CHECKING:
    from ai_coding.ui.textual_app import AICodingApp

SLASH_COMMANDS = [
    ("/plan", "Create and refine an implementation plan"),
    ("/review", "Review current changes and surface issues"),
    ("/status", "Show session and runtime status"),
    ("/model", "List or switch models"),
    ("/resume", "List or resume saved sessions"),
    ("/permissions", "View or change approval policy"),
    ("/todo", "Show the current task list"),
    ("/skills", "List available skills"),
    ("/config", "Show effective configuration"),
    ("/compact", "Compact conversation history"),
    ("/memory", "Show memory status"),
    ("/trace", "Show the last turn's trace"),
    ("/budget", "Show session token totals"),
    ("/clear", "Start a fresh session"),
    ("/help", "Show available commands"),
    ("/quit", "Exit the app"),
]


class SlashCommandProvider(Provider):
    """Fuzzy-searches the app's slash commands.

    No ``__init__``: the palette instantiates providers as
    ``provider_class(screen, match_style)`` (command.py:828).
    """

    async def discover(self) -> AsyncIterator[DiscoveryHit]:
        """The palette's initial (empty-query) view: every command."""
        for name, help_text in SLASH_COMMANDS:
            yield DiscoveryHit(plain(name), self._run(name), help=help_text)

    async def search(self, query: str) -> AsyncIterator[Hit]:
        if not query:
            # The fuzzy matcher raises ValueError on empty input in 8.2.8;
            # the palette routes empty queries to discover(), so this is
            # defensive — but it must still yield Hits, not DiscoveryHits.
            for name, help_text in SLASH_COMMANDS:
                yield Hit(1.0, plain(name), self._run(name), help=help_text)
            return
        matcher = self.matcher(query)
        for name, help_text in SLASH_COMMANDS:
            if (score := matcher.match(name)) > 0:
                yield Hit(score, matcher.highlight(name), self._run(name), help=help_text)

    def _run(self, name: str) -> Callable[[], None]:
        app = cast("AICodingApp", self.app)

        def callback() -> None:
            app.submit_slash(name)

        return callback
