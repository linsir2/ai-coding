"""Rich console wrapper for the REPL (M5-2).

Thin, mostly-styling layer on top of ``rich.console.Console`` so the interactive
loop and its components can share consistent coloring without coupling to
``typer.echo``.  ``record=True`` consoles can be inspected in tests.
"""

from __future__ import annotations

from rich.console import Console

_ANSWER_STYLE = "cyan"
_INFO_STYLE = "green"
_ERROR_STYLE = "bold red"


class DialogueConsole:
    """Render REPL output (assistant deltas, info, errors) with rich."""

    def __init__(self, console: Console | None = None) -> None:
        self._c = console or Console()

    def delta(self, token: str) -> None:
        """Emit one assistant output token (no trailing newline)."""
        self._c.print(token, end="", style=_ANSWER_STYLE)

    def info(self, message: str) -> None:
        self._c.print(message, style=_INFO_STYLE)

    def error(self, message: str) -> None:
        self._c.print(message, style=_ERROR_STYLE)

    def rule(self, title: str = "") -> None:
        self._c.rule(title)


__all__ = ["DialogueConsole"]
