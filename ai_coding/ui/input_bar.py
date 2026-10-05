"""Bottom input: rounded box, placeholder, hint line."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input

from ai_coding.ui.plain import Plain

PLACEHOLDER = "Type a message or enter a command..."
HINT = "/ for commands"


class InputBar(Vertical):
    """The input row plus the ``/ for commands`` hint under it."""

    def __init__(self) -> None:
        super().__init__(id="input-bar")

    def compose(self) -> ComposeResult:
        with Horizontal(id="input-row"):
            yield Input(placeholder=PLACEHOLDER, id="prompt")
        yield Plain(HINT, id="input-hint")

    @property
    def prompt(self) -> Input:
        return self.query_one("#prompt", Input)
