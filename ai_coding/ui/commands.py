"""Slash-command provider for the Textual command palette."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, cast

from textual.command import Hit, Provider

if TYPE_CHECKING:
    from ai_coding.ui.textual_app import AICodingApp

SLASH_COMMANDS = [
    ("plan", "Create and refine an implementation plan"),
    ("review", "Review current changes and surface issues"),
    ("status", "Show session and runtime status"),
    ("model", "List or switch models"),
    ("resume", "List or resume saved sessions"),
    ("permissions", "View or change approval policy"),
    ("todo", "Show the current task list"),
    ("skills", "List available skills"),
    ("config", "Show effective configuration"),
    ("compact", "Compact conversation history"),
    ("memory", "Show memory status"),
    ("trace", "Show the last turn's trace"),
    ("budget", "Show session token totals"),
    ("clear", "Start a fresh session"),
    ("help", "Show available commands"),
    ("quit", "Exit the app"),
]


class SlashCommandProvider(Provider):
    async def search(self, query: str) -> AsyncIterator[Hit]:
        matcher = self.matcher(query)
        for name, help_text in SLASH_COMMANDS:
            if (score := matcher.match(name)) > 0:
                yield Hit(score, matcher.highlight(name), self._run(name), help=help_text)

    def _run(self, name: str) -> Callable[[], None]:
        def callback() -> None:
            app = cast("AICodingApp", self.app)
            app.run_command(f"/{name}")

        return callback
