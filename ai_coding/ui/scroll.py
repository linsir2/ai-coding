"""Mouse-wheel scrolling for the body window.

R11: the wheel is bound to prompt_toolkit's own ``Window._scroll_up`` /
``_scroll_down`` rather than to ``render_info.vertical_scroll``.  That field is
private, has no bounds clamping, and is ``None`` until the first render — the
helpers below tolerate exactly that, so a wheel event arriving before the first
paint is a no-op instead of an ``AttributeError``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["scroll_up", "scroll_down", "auto_scroll_to_end"]


def scroll_up(window: Any) -> None:
    """Wheel up: move the body toward older output.

    ``Window._scroll_up`` already no-ops before the first render, so there is no
    guard here: an earlier version returned early when ``render_info`` was
    ``None``, which also swallowed every real wheel event before the first
    paint of a freshly started session.
    """
    window._scroll_up()


def scroll_down(window: Any) -> None:
    """Wheel down: move the body toward newer output."""
    window._scroll_down()


def auto_scroll_to_end(window: Any) -> None:
    """Pin the body to the newest line after new output arrives.

    Without this, streaming output scrolls the view away from the cursor the
    moment the buffer outgrows the window.

    ``_scroll_down`` is the right primitive rather than a computed row index:
    the body buffer's cursor always sits at the newest character, and
    ``_scroll_down`` clamps to "keep the cursor visible" — so it lands on the
    last line and no further.  ``Window._scroll`` is *not* this operation; it is
    an internal render-time helper taking ``(ui_content, width, height)``.
    """
    window._scroll_down()
