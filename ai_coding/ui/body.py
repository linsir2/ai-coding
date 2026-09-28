"""Scrollable output Buffer.

R12: assistant text arrives token-by-token through ``on_delta``, so Markdown
decoration cannot be stripped per ``write`` call — a ``**bold**`` pair is
normally split across two tokens and would never match.  Text is therefore
assembled per line and stripped when the newline lands, which is also what
makes ``get_line_prefix`` able to see the *original* line and pick a style
from it.
"""

from __future__ import annotations

from prompt_toolkit.buffer import Buffer

from ai_coding.ui.markdown import strip_inline, strip_leading


def _render_line(raw: str) -> str:
    """The text the body shows for one written line.

    Leading markers first (``### ``, ``- ``, a fence) then inline ones
    (``**bold**``, `` `code` ``) — the order matters, because a bullet line
    whose text is bolded needs both passes.
    """
    return strip_inline(strip_leading(raw))


class BodyBuffer:
    def __init__(self) -> None:
        self._buffer = Buffer()
        self._partial = ""
        #: Original (unstripped) text per completed line, parallel to the buffer.
        #: The renderer needs it because the buffer holds the *stripped* line and
        #: can no longer tell a heading from ordinary prose.
        self._raw_lines: list[str] = []

    def write(self, text: str, style: str = "") -> None:
        """Append ``text``, stripping Markdown once each line is complete.

        ``style`` is accepted for call-site symmetry with the rest of the UI but
        the body is a plain ``Buffer``: it carries one style per line, decided at
        render time by ``get_line_prefix``, not per ``write``.
        """
        if not text:
            return
        self._partial += text
        if "\n" not in self._partial:
            return
        complete, _, rest = self._partial.rpartition("\n")
        self._partial = rest
        for line in complete.split("\n"):
            self._buffer.insert_text(_render_line(line) + "\n")
            self._raw_lines.append(line)

    def flush(self) -> None:
        """Emit the trailing line that has no newline yet (end of a turn)."""
        if self._partial:
            self._buffer.insert_text(_render_line(self._partial))
            self._raw_lines.append(self._partial)
            self._partial = ""

    def raw_line(self, index: int) -> str:
        """The line as it was written, before Markdown was stripped."""
        return self._raw_lines[index] if index < len(self._raw_lines) else ""

    def clear(self) -> None:
        self._buffer.reset()
        self._partial = ""
        self._raw_lines.clear()

    def get_buffer(self) -> Buffer:
        return self._buffer

    def get_formatted_text(self) -> str:
        self.flush()
        return self._buffer.text
