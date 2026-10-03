"""The UI's text boundary: untrusted text must not be parsed as markup.

Textual's `Static`, `OptionList` and `Label` (and rich's `Console.print`) treat a
`str` as console markup by default. Program output, tool parameters, the user's
own message and saved-session previews all contain `[` characters, and markup
parsing either eats them (`build [k=v]` renders as `build `) or aborts the app
(`MarkupError: Expected markup value` on `[path=/tmp/x]`).

Two shapes of defence, because one is not enough:

* `Plain` — a `Static` with markup permanently off, for places that *render* a
  string. It works because `visualize()` only parses `str`.
* `plain()` — wraps a string as `Content`, for channels that carry *data* and
  take no options (`Option.prompt` inside the command palette is the one that
  bites: the palette builds its own `OptionList`, so its `markup=True` is not
  ours to change).

`tests/test_ui_text_boundary.py` enforces both by AST, so the next rendering
point cannot quietly reopen the hole.
"""

from __future__ import annotations

from typing import Any

from textual.content import Content
from textual.widgets import Static


class Plain(Static):
    """``Static`` that never interprets its content as markup."""

    def __init__(
        self,
        content: Any = "",
        *,
        expand: bool = False,
        shrink: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            content,
            expand=expand,
            shrink=shrink,
            markup=False,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )


def plain(value: str) -> Content:
    """Carry untrusted text through a markup-parsing channel verbatim.

    `Content` is a `Visual`, so `visualize()` hands it straight to the renderer
    instead of running the markup parser.  Only for display values — search
    keys (e.g. `Hit.text`) must stay `str`, or the fuzzy matcher breaks.
    """
    return Content(value)


__all__ = ["Plain", "plain"]
