"""The Textual application: shell now, loop wiring in Task 5."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding

from ai_coding.ui.input_bar import InputBar
from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.transcript import Transcript


class AICodingApp(App[None]):
    """Full-screen coding assistant UI."""

    CSS_PATH = "app.tcss"
    # Textual binds ctrl+q to quit and ctrl+c to "copy or hint".
    # spec §4 #14: keep the old ctrl+c quit (fires only when nothing is selected,
    # because Screen's copy binding has priority and raises SkipAction otherwise).
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    # Declared here (not only assigned in on_mount) so mypy strict can infer them.
    _loop: Any
    _session: Any
    _sessions: Any
    _workspace: str
    _model_name: str

    def __init__(
        self,
        *,
        loop: Any,
        session: Any,
        sessions: Any,
        workspace: str,
        model_name: str = "",
    ) -> None:
        super().__init__()
        self._loop = loop
        self._session = session
        self._sessions = sessions
        self._workspace = workspace
        self._model_name = model_name

    def compose(self) -> ComposeResult:
        yield StatusBar(self._workspace, self._model_name)
        yield Transcript(id="transcript")
        yield InputBar()
