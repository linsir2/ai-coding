"""One-row status bar: ``>_ workspace`` left, model + state right."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal

from ai_coding.ui.plain import Plain


class StatusBar(Horizontal):
    """Fixed one-row bar at the top of the app."""

    def __init__(self, workspace: str, model_name: str = "") -> None:
        super().__init__(id="status-bar")
        self._workspace = workspace
        self._model = model_name
        self._busy = False
        #: The two halves, held rather than looked up by id.  ``_refresh`` runs
        #: from a turn worker, and a screen transition (a modal pushes and pops)
        #: can leave this bar detached for a moment -- a lookup then raises
        #: ``NoMatches`` out of the worker and kills the command it was running.
        #: A refresh on a detached bar updates nothing, which is exactly right.
        self._left: Plain | None = None
        self._right: Plain | None = None

    def compose(self) -> ComposeResult:
        self._left = Plain(id="status-left")
        self._right = Plain(id="status-right")
        yield self._left
        yield self._right

    def on_mount(self) -> None:
        self._refresh()

    def set_model(self, name: str) -> None:
        self._model = name
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def _refresh(self) -> None:
        if self._left is None or self._right is None:
            return  # not composed yet (``set_busy`` can arrive first)
        left = Text()
        left.append(">_ ", style="bold")
        left.append(self._workspace)
        if self._model:
            left.append(f"  ·  {self._model}", style="dim")
        self._left.update(left)
        self._right.update(
            Text(
                "● Working" if self._busy else "● Ready",
                style="yellow" if self._busy else "green",
            )
        )
