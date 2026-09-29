"""Modal screens for the two human channels: approval and ask_user.

Both return the *strings the existing parsers expect*: approval answers are
parsed by ``security/approval.py`` (``y``/``n``/``s``), and an empty ask answer
becomes ``UserAnswer(reason="empty")`` → the tool tells the model to decide
(spec §4.2).  Escape therefore returns ``""``, never raises.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class ApprovalScreen(ModalScreen[str]):
    """Ask whether one tool call may proceed."""

    BINDINGS = [Binding("escape", "no", "No", show=False)]

    def __init__(self, tool_name: str, params: dict[str, Any], reason: str) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._reason = reason

    def compose(self) -> ComposeResult:
        body = Text()
        body.append(f"{self._tool_name}\n", style="bold")
        if self._reason:
            body.append(f"{self._reason}\n", style="yellow")
        preview = str(self._params)
        body.append(preview if len(preview) <= 200 else preview[:197] + "...", style="dim")
        with Vertical(id="approval-box"):
            yield Static(body, id="approval-body")
            with Horizontal(id="approval-buttons"):
                yield Button("Allow", id="yes", variant="success")
                yield Button("Deny", id="no", variant="error")
                yield Button("Stop", id="stop", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # ``event.button.id`` is ``str | None``; an unknown button fails closed.
        self.dismiss({"yes": "y", "no": "n", "stop": "s"}.get(event.button.id or "", "n"))

    def action_no(self) -> None:
        self.dismiss("n")


class AskScreen(ModalScreen[str]):
    """Ask the human a free-form question (options are shown, not enforced)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, question: str, options: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._question = question
        self._options = tuple(options)

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-box"):
            yield Static(Text(self._question), id="ask-question")
            if self._options:
                yield Static(Text("可选：" + " / ".join(self._options)), id="ask-options")
            yield Input(id="ask-input")
            yield Button("Send", id="ask-send", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#ask-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._send()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # The modal owns its input: stop the Submitted message so an App-level
        # handler (the main prompt, Task 5) never sees a modal answer as a turn.
        event.stop()
        self._send()

    def _send(self) -> None:
        self.dismiss(self.query_one("#ask-input", Input).value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")
