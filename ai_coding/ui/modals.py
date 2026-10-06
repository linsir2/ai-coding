"""Modal screens for the two human channels: approval and ask_user.

Both return the *strings the existing parsers expect*: approval answers are
parsed by ``security/approval.py`` (``y``/``n``/``s``), and an empty ask answer
becomes ``UserAnswer(reason="empty")`` → the tool tells the model to decide
(spec §4.2).  Escape therefore returns ``""``, never raises.

``ApprovalScreen`` takes the gate's structured call — ``tool_name``, ``params``,
``reason`` — rather than the formatted prompt string.  It used to re-parse that
string, and the prompt truncates ``params`` at 120 characters, so anything longer
reached the operator as nothing at all.
"""

from __future__ import annotations

import difflib
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input

from ai_coding.ui.plain import Plain

_DIFF_LIMIT = 200

#: Tools that never get an "Always" grant: on bash that would approve every
#: command, which is the one grant that must not exist.  The screen hides the
#: button and the provider refuses to record, so both read this.
ALWAYS_EXCLUDED = frozenset({"bash"})


def _edit_diff(params: dict[str, Any]) -> list[str]:
    """An edit's old/new as unified-diff lines, ready to append to a ``Text``.

    The operator judges the *change*, so ``old_string = ...`` / ``new_string =
    ...`` is the wrong shape to read.  ``difflib`` is stdlib; the cap keeps a
    whole-file rewrite from pinning the modal.
    """
    old = str(params.get("old_string", ""))
    new = str(params.get("new_string", ""))
    if not old and not new:
        return []
    # ``keepends=True`` + default ``lineterm`` is difflib's own idiom; it only
    # terminates the ``---``/``+++``/``@@`` headers, so a last line without a
    # trailing newline would still run into the next diff line.
    lines = [
        line if line.endswith("\n") else line + "\n"
        for line in difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="原",
            tofile="新",
        )
    ]
    if len(lines) > _DIFF_LIMIT:
        lines = lines[:_DIFF_LIMIT] + [f"…（diff 过长，仅显示前 {_DIFF_LIMIT} 行）\n"]
    return lines


class ApprovalScreen(ModalScreen[str]):
    """Ask whether one tool call may proceed.

    Keyboard-first, matching the CLI's ``[y/N/s]``: ``y`` allows, ``n`` denies,
    ``s`` stops, Escape denies.  The bindings live on the *screen* because a
    focused ``Button`` swallows character keys — with the default focus on Allow,
    typing "n" would have been consumed as a keypress and then activated Allow.
    Focus therefore starts on Deny, so an accidental Enter denies.
    """

    BINDINGS = [
        Binding("escape", "no", "Deny", show=False),
        Binding("y", "yes", "Allow", show=False),
        Binding("n", "no", "Deny", show=False),
        Binding("s", "stop", "Stop", show=False),
        Binding("a", "always", "Always allow", show=False),
    ]

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
        if self._tool_name == "edit":
            # An edit is judged by what changed, not by parameter names: show
            # the path and a unified diff of old_string -> new_string.
            path = str(self._params.get("path", ""))
            if path:
                body.append(f"edit {path}\n", style="bold")
            if self._params.get("replace_all"):
                # The diff cannot show this: one hunk rendered identically turns
                # a single replacement into a whole-file rewrite.
                body.append("replace_all: 替换文件内全部匹配\n", style="yellow")
            diff = _edit_diff(self._params)
            for line in diff:
                if line.startswith(("---", "+++")):
                    body.append(line, style="dim")
                elif line.startswith("-"):
                    body.append(line, style="red")
                elif line.startswith("+"):
                    body.append(line, style="green")
                else:
                    body.append(line, style="dim")
        elif self._params:
            # Not truncated: the whole point of the box is that the operator
            # reads what they are about to allow.  ``#approval-body`` wraps and
            # scrolls, so a long bash command costs a scrollbar, not a decision
            # made blind.
            #
            # One key per line rather than ``str(params)``: the operator is
            # judging what the call will do, and dict repr spends the width on
            # braces, quotes and commas between the key and the value.
            body.append(
                "\n".join(f"{key} = {value}" for key, value in self._params.items()),
                style="dim",
            )
        with Vertical(id="approval-box"):
            # The scroll view wraps the Static rather than becoming it: a Static
            # reports a virtual size clamped to its max-height, so it silently
            # crops instead of scrolling.  The id stays on the Static so the
            # text is still reachable as one widget.
            with VerticalScroll(id="approval-body-scroll"):
                yield Plain(body, id="approval-body")
            with Horizontal(id="approval-buttons"):
                yield Button("Allow", id="yes", variant="success")
                yield Button("Deny", id="no", variant="error")
                yield Button("Stop", id="stop", variant="warning")
                # bash is excluded from "always" (ALWAYS_EXCLUDED): an
                # always-allow on bash would approve every command, so the
                # button is not offered for it.
                if self._tool_name not in ALWAYS_EXCLUDED:
                    yield Button("Always", id="always", variant="primary")
            keys = "y allow · n deny · s stop"
            if self._tool_name not in ALWAYS_EXCLUDED:
                keys += " · a always"
            yield Plain(f"{keys} · esc deny", id="approval-hint")

    def on_mount(self) -> None:
        # Deny, not Allow: whichever answer a reflexive keypress produces, it
        # has to be the one that does not run the tool.
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # ``event.button.id`` is ``str | None``; an unknown button fails closed.
        self.dismiss(
            {"yes": "y", "no": "n", "stop": "s", "always": "a"}.get(
                event.button.id or "", "n"
            )
        )

    def action_yes(self) -> None:
        self.dismiss("y")

    def action_no(self) -> None:
        self.dismiss("n")

    def action_stop(self) -> None:
        self.dismiss("s")

    def action_always(self) -> None:
        # The key is bound on every tool, so an excluded one has to refuse here:
        # dismissing ``"a"`` for bash would auto-approve the command without ever
        # recording it, which is the grant ALWAYS_EXCLUDED exists to prevent.
        if self._tool_name in ALWAYS_EXCLUDED:
            return
        self.dismiss("a")


class AskScreen(ModalScreen[str]):
    """Ask the human a free-form question.

    ``options`` is rendered under the question when a caller supplies it; they
    are shown, not enforced.  Until the provider passes them through, every
    caller leaves it empty and the line does not appear.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, question: str, options: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._question = question
        self._options = tuple(options)

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-box"):
            yield Plain(Text(self._question), id="ask-question")
            if self._options:
                yield Plain(Text("可选：" + " / ".join(self._options)), id="ask-options")
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
