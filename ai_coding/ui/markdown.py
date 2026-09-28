"""Strip Markdown decoration for the plain-text body.

R12: the body is a ``prompt_toolkit`` ``Buffer`` — it renders one style per
*line* (via ``get_line_prefix``), so it cannot show a bold run inside a line.
Anything left in the text therefore reads as raw markup rather than as
formatting.  These helpers remove the decoration and leave the words, and
``app.py`` picks the line style from the *original* line, so a heading still
gets its heading colour.

``strip_inline`` is deliberately conservative: a lone ``*`` (multiplication,
a bullet in prose, a half-streamed marker) is left alone, because eating the
rest of a line is far worse than showing one stray character.
"""

from __future__ import annotations

import re

__all__ = ["strip_inline", "strip_leading"]

#: ``**bold**`` / ``__bold__`` with a non-space on one side so a bare ``*`` survives.
_BOLD = re.compile(r"(?<!\S)(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
#: `` `code` `` — only paired fences are removed.
_CODE = re.compile(r"(?<!\S)`(?=\S)(.+?)(?<=\S)`")
#: ``#``-run at the start of a line, i.e. a heading.
_HEADING = re.compile(r"^#{1,6}\s*")
#: A list bullet, normalised so every list reads the same.
_BULLET = re.compile(r"^(\s*)[-*]\s+")
#: A code fence line renders as nothing — the body has no room for it.
_FENCE = re.compile(r"^\s*```")


def strip_inline(text: str) -> str:
    """Remove ``**bold**``, ``__bold__`` and paired `` `code` `` from ``text``."""
    out = _BOLD.sub(lambda m: m.group(2), text)
    return _CODE.sub(lambda m: m.group(1), out)


def strip_leading(line: str) -> str:
    """Remove a heading's ``#`` run, a list marker's dash, or a whole code fence.

    Returns ``""`` for a fence line, which is how a fence is recognised: the
    body draws no box, so the fence characters would just be noise.
    """
    if _FENCE.match(line):
        return ""
    line = _HEADING.sub("", line)
    return _BULLET.sub(lambda m: f"{m.group(1)}• ", line)
