"""Console interface written through by ``handle_input`` and the Textual app.

A thin four-method surface (``delta`` / ``info`` / ``error`` / ``rule``) so the
slash-command core in ``ui/repl.py`` and the command handlers don't couple to
``typer.echo`` or to the TUI.  ``DialogueConsole`` renders to a rich console
(``record=True`` consoles can be inspected in tests); ``TextualConsole`` writes
into the Textual transcript instead.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

_ANSWER_STYLE = "cyan"
_INFO_STYLE = "green"
_ERROR_STYLE = "bold red"


class DialogueConsole:
    """Render assistant deltas, info, errors and rules with rich."""

    def __init__(self, console: Console | None = None) -> None:
        self._c = console or Console()

    def delta(self, token: str) -> None:
        """Emit one assistant output token (no trailing newline)."""
        # markup=False on every print: the text is the model's / the user's
        # words, and rich would parse (and eat or reject) any ``[`` in it.
        self._c.print(token, end="", style=_ANSWER_STYLE, markup=False)

    def info(self, message: str) -> None:
        self._c.print(message, style=_INFO_STYLE, markup=False)

    def error(self, message: str) -> None:
        self._c.print(message, style=_ERROR_STYLE, markup=False)

    def rule(self, title: str = "") -> None:
        self._c.rule(title)


class TextualConsole(DialogueConsole):
    """DialogueConsole that writes into the Textual transcript instead of stdout.

    Subclassing (rather than duck-typing) keeps ``repl.py``'s annotations true
    without touching that file.
    """

    def __init__(self, transcript: Any = None) -> None:
        # Intentionally does not call super().__init__: no rich Console needed.
        # The default keeps the override Liskov-compatible with DialogueConsole
        # (mypy strict); the app always passes a transcript.
        self._transcript = transcript
        self._streaming = False

    def delta(self, token: str) -> None:
        if not self._streaming:
            from datetime import datetime

            self._transcript.begin_assistant(datetime.now().strftime("%H:%M"))
            self._streaming = True
        self._transcript.stream(token)

    def _end_stream(self) -> None:
        if self._streaming:
            self._transcript.flush()
            self._streaming = False

    def info(self, message: str) -> None:
        self._end_stream()
        from ai_coding.ui.blocks import Block

        self._transcript.append_block(Block("info", message))

    def error(self, message: str) -> None:
        self._end_stream()
        from ai_coding.ui.blocks import Block

        self._transcript.append_block(Block("turn-end", message))

    def rule(self, title: str = "") -> None:
        self._end_stream()
        if title:
            from ai_coding.ui.blocks import Block

            self._transcript.append_block(Block("turn", title))


__all__ = ["DialogueConsole", "TextualConsole"]
