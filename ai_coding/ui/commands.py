"""Slash-command provider for the Textual command palette."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, cast

from textual.command import DiscoveryHit, Hit, Provider

from ai_coding.ui.plain import plain

if TYPE_CHECKING:
    from ai_coding.domain.session import SessionData
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


class ResumeProvider(Provider):
    """Lists saved sessions by recognizable content, not bare IDs.

    No ``__init__``: the palette instantiates providers as
    ``provider_class(screen, match_style)`` (command.py:828).
    """

    async def discover(self) -> AsyncIterator[DiscoveryHit]:
        """The picker's initial view: every saved session."""
        for session in self._sessions():
            display = session.first_preview
            if len(display) > 80:
                display = display[:77] + "..."
            date = session.last_access_time[:10] if len(session.last_access_time) >= 10 else ""
            help_text = f"{session.session_id[:12]} · {session.message_count} msgs"
            if date:
                help_text += f" · {date}"
            # ``plain()``: the preview is the user's own words, so it can contain
            # ``[`` — and the palette's OptionList parses display values as
            # markup.  ``Hit.text`` stays a str: it feeds the matcher, not a
            # renderer, and wrapping it would break `/resume` search.
            yield DiscoveryHit(
                plain(display), self._resume(session.session_id), help=help_text
            )

    async def search(self, query: str) -> AsyncIterator[Hit]:
        """Fuzzy-filter on both the preview text and the session id.

        The id is searched because that is what ``/resume <id>`` takes, so a
        user who knows the id must still be able to reach it from the picker.
        """
        matcher = self.matcher(query)
        for session in self._sessions():
            preview = session.first_preview
            best = matcher.match(preview)
            display = preview
            if (score := matcher.match(session.session_id[:12])) > best:
                best = score
                display = f"{session.session_id[:12]}"
            if best <= 0:
                continue
            yield Hit(
                best,
                plain(display),
                self._resume(session.session_id),
                text=preview or None,
            )

    def _sessions(self) -> list[SessionData]:
        """Saved sessions except the one already open.

        Resuming the open conversation is a no-op, so it is not offered.
        """
        app = cast("AICodingApp", self.app)
        current_id = getattr(getattr(app, "_session", None), "session_id", None)
        return [
            s
            for s in app._sessions.list_sessions()
            if current_id is None or s.session_id != current_id
        ]

    def _resume(self, session_id: str) -> Callable[[], None]:
        """Resume by full ID — the list only ever shows a 12-char prefix."""
        app = cast("AICodingApp", self.app)

        def callback() -> None:
            app.submit_slash(f"/resume {session_id}")

        return callback
