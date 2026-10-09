"""Console interface written through by ``handle_input``.

A thin four-method surface (``delta`` / ``info`` / ``error`` / ``rule``) so the
slash-command core in ``app/commands.py`` and the command handlers don't couple
to ``typer.echo`` or to the TUI.  ``DialogueConsole`` renders to a rich console
(``record=True`` consoles can be inspected in tests).
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

_ANSWER_STYLE = "cyan"
_INFO_STYLE = "green"
_ERROR_STYLE = "bold red"


class _LineSink:
    """A step view for consoles that have no folding: writes the deltas out.

    The REPL prints the step's words as they arrive instead of folding them —
    a terminal has no widget to collapse, and hiding the work behind a
    one-line summary would be exactly the invisibility M3 exists to remove.
    """

    def __init__(self, console: DialogueConsole) -> None:
        self._console = console

    def delta(self, text: str) -> None:
        self._console.delta(text)

    def flush(self) -> bool:
        return False

    def end(self, summary: str | None = None) -> None:
        if summary:
            self._console.info(f"  → {summary}")


class DialogueConsole:
    """Render assistant deltas, info, errors and rules with rich."""

    def __init__(self, console: Console | None = None) -> None:
        self._c = console or Console()

    def delta(self, token: str) -> None:
        """Emit one assistant output token (no trailing newline)."""
        # markup=False on every print: the text is the model's / the user's
        # words, and rich would parse (and eat or reject) any ``[`` in it.
        self._c.print(token, end="", style=_ANSWER_STYLE, markup=False)

    def step(self, title: str) -> Any:
        """Open a step view (M3).  No folding here: the REPL prints the work."""
        self.info(title)
        return _LineSink(self)

    def ask(self, prompt: str) -> str | None:
        """Ask the user a question on the terminal; ``None`` if they decline.

        ``None`` for an empty answer *and* for EOF, so Ctrl-D at a prompt ends
        the question instead of raising out of the command handler.
        """
        from rich.prompt import Prompt

        try:
            return Prompt.ask(prompt, console=self._c, default="") or None
        except (EOFError, KeyboardInterrupt):
            return None

    def info(self, message: str) -> None:
        self._c.print(message, style=_INFO_STYLE, markup=False)

    def error(self, message: str) -> None:
        self._c.print(message, style=_ERROR_STYLE, markup=False)

    def rule(self, title: str = "") -> None:
        self._c.rule(title)


__all__ = ["DialogueConsole"]
