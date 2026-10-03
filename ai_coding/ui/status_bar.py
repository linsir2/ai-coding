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

    def compose(self) -> ComposeResult:
        yield Plain(id="status-left")
        yield Plain(id="status-right")

    def on_mount(self) -> None:
        self._refresh()

    def set_model(self, name: str) -> None:
        self._model = name
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def _refresh(self) -> None:
        left = Text()
        left.append(">_ ", style="bold")
        left.append(self._workspace)
        if self._model:
            left.append(f"  ·  {self._model}", style="dim")
        self.query_one("#status-left", Plain).update(left)
        self.query_one("#status-right", Plain).update(
            Text("● Working" if self._busy else "● Ready",
                 style="yellow" if self._busy else "green")
        )
