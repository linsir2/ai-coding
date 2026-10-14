"""HTML -> text / markdown, pure (r17/T3).

Fetched web pages are written for browsers, not for a model's context: they
carry scripts, styles, navigation and footers around a small amount of content.
This converts them to something consumable, and drops the chrome entirely.

Two tag sets, mirroring OpenCode's split (``html-markdown.ts:3``) but stricter on
chrome: ``SKIP`` never contributes text at all; ``BOILERPLATE`` (nav/header/
footer/aside/form) is dropped too — OpenCode only line-breaks those, and the
operator asked for content that can be *consumed*, so a menu is noise either
way.  Pure functions: no network, no filesystem, fully testable.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

#: Void elements that carry no text and have no end tag: dropping them must not
#: enter the suppression counter, or everything after them is suppressed forever
#: (a real example.com fetch found exactly that).
_VOID = frozenset({"meta", "link", "base"})
#: Container tags whose whole subtree never contributes text.
_SKIP = frozenset(
    {"script", "style", "noscript", "iframe", "object", "embed", "template", "svg"}
)
#: Chrome: dropped with its subtree rather than line-broken.
_BOILERPLATE = frozenset({"nav", "header", "footer", "aside", "form"})
#: Structure: contributes a line break (and markdown syntax where it has one).
_BLOCK = frozenset(
    {
        "p", "div", "section", "article", "main", "address", "figure", "figcaption",
        "details", "summary", "dl", "dt", "dd", "fieldset", "blockquote",
        "ul", "ol", "li", "table", "tr", "td", "th", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }
)
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_INLINE_CODE = {"code", "kbd", "samp", "tt"}


class _Extractor(HTMLParser):
    def __init__(self, *, markdown: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.markdown = markdown
        self.parts: list[str] = []
        self._suppressed = 0
        self._links: list[str] = []
        # Inside <pre> the source's own whitespace is the content.
        self._pre_depth = 0

    # -- tags ---------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        if tag in _SKIP or tag in _BOILERPLATE:
            self._suppressed += 1
            return
        if self._suppressed:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._links.append(href)
            if self.markdown and href:
                self.parts.append("[")
        elif tag == "li":
            self.parts.append("\n- " if self.markdown else "\n")
        elif tag in _HEADINGS:
            self.parts.append("\n\n" + ("#" * int(tag[1]) + " " if self.markdown else ""))
        elif tag in _INLINE_CODE:
            if self.markdown:
                self.parts.append("`")
        elif tag == "pre":
            self._pre_depth += 1
            if self.markdown:
                self.parts.append("\n```\n")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if tag in _SKIP or tag in _BOILERPLATE:
            self._suppressed = max(0, self._suppressed - 1)
            return
        if self._suppressed:
            return
        if tag == "a":
            href = self._links.pop() if self._links else ""
            if self.markdown and href:
                self.parts.append(f"]({href})")
        elif tag in _INLINE_CODE:
            if self.markdown:
                self.parts.append("`")
        elif tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            if self.markdown:
                self.parts.append("\n```\n")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppressed:
            return
        # Source newlines inside a block are just whitespace (browser semantics);
        # only the block markers above create line breaks.  <pre> is exempt.
        self.parts.append(data if self._pre_depth else re.sub(r"\s+", " ", data))


def _clean(parts: list[str]) -> str:
    """Trim line ends and drop blank-line runs; block breaks are the newlines.

    Whitespace *inside* a line was already collapsed when the data arrived, so
    this only cleans the seams between blocks.  ``rstrip`` (not ``strip``)
    keeps a ``<pre>`` block's indentation.
    """
    lines = [line.rstrip() for line in "".join(parts).split("\n")]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip()


def html_to_text(html: str) -> str:
    """Visible text, one block per line, no markup."""
    parser = _Extractor(markdown=False)
    parser.feed(html)
    parser.close()
    return _clean(parser.parts)


def html_to_markdown(html: str) -> str:
    """Text with headings, lists, links and code kept as markdown."""
    parser = _Extractor(markdown=True)
    parser.feed(html)
    parser.close()
    return _clean(parser.parts)


__all__ = ["html_to_markdown", "html_to_text"]
