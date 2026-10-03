"""Session picker screen: grouped, searchable, previews.

Bare ``/resume`` pushes this modal instead of the Textual command palette.
A palette renders one hit per row sorted by score — it cannot carry
day-group headers — so the old ``ResumeProvider`` path is gone.  Every
external string goes through ``plain()``: previews are the user's own words,
and the palette path died on ``[`` more than once.
"""

from __future__ import annotations

from textual import on
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from ai_coding.domain.session import SessionData
from ai_coding.ui.plain import Plain, plain
from ai_coding.ui.resume import filter_sessions, group_sessions_by_day, local_hhmm

_PREVIEW_LIMIT = 80


def _row_text(session: SessionData) -> str:
    """One row: local time, recognizable preview, message count.

    A session with no preview and no title still shows its id prefix — a
    blank row is how the old list became unreadable.
    """
    preview = (session.first_preview or session.title or session.session_id[:12]).strip()
    if len(preview) > _PREVIEW_LIMIT:
        preview = preview[:77] + "..."
    count = session.message_count
    msgs = f" · {count} msgs" if isinstance(count, int) and count >= 0 else ""
    stamp = local_hhmm(session.last_access_time)
    head = f"{stamp} · " if stamp else ""
    return f"{head}{preview}{msgs}"


class ResumeScreen(ModalScreen[str | None]):
    """Pick a saved session.  Dismisses with the session id, or ``None``."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, sessions: list[SessionData], *, exclude_id: str | None = None) -> None:
        super().__init__()
        self._sessions = [s for s in sessions if s.session_id != exclude_id]

    def compose(self):  # type: ignore[no-untyped-def]
        yield Input(placeholder="Search sessions…", id="resume-search")
        yield OptionList(id="resume-list")
        yield Plain("enter 打开 · esc 取消", id="resume-hint")

    def on_mount(self) -> None:
        self._rebuild("")
        self.query_one("#resume-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "resume-search":
            self._rebuild(event.value)

    def _rebuild(self, query: str) -> None:
        items = self.query_one("#resume-list", OptionList)
        items.clear_options()
        for label, group in group_sessions_by_day(filter_sessions(self._sessions, query)):
            items.add_option(Option(plain(label), disabled=True))
            for session in group:
                items.add_option(Option(plain(_row_text(session)), id=session.session_id))

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["ResumeScreen"]
