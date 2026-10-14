"""TDD (r17/T3): fetched HTML must be cleaned before the model reads it.

Scripts/styles are dropped (OpenCode's html-markdown.ts:3 lists the same set);
navigation/footer/chrome are dropped too — a stronger clean than OpenCode's
line-break treatment, because the point here is *consumable* content, which is
what the operator asked for.  Headings/lists/links survive in markdown.  Pure
functions: no network, no filesystem.
"""

from __future__ import annotations

from ai_coding.tools.htmltext import html_to_markdown, html_to_text


def test_scripts_styles_and_chrome_never_reach_the_model():
    html = (
        "<html><head><style>x{}</style><script>alert(1)</script></head>"
        "<body><nav>menu</nav><header>top</header>"
        "<p>Real content.</p><footer>bottom</footer></body></html>"
    )

    out = html_to_text(html)

    assert "Real content." in out
    for junk in ("alert", "x{}", "menu", "top", "bottom"):
        assert junk not in out


def test_markdown_keeps_structure():
    html = (
        "<h2>Title</h2><p>one <b>two</b></p>"
        "<ul><li>a</li><li>b</li></ul>"
        "<a href='https://e.com'>e</a>"
    )

    md = html_to_markdown(html)

    assert "## Title" in md
    assert "one two" in md
    assert "- a" in md and "- b" in md
    assert "[e](https://e.com)" in md


def test_whitespace_is_collapsed_and_blank_runs_are_removed():
    html = "<p>a\n\n   b</p>\n\n\n<p>c</p>"

    out = html_to_text(html)

    assert "a b" in out
    assert "\n\n\n" not in out


def test_a_realistic_head_with_void_elements_does_not_suppress_the_body():
    """Regression: a real page's <head> is full of void tags (meta/link) with no
    end tag.  Counting them as suppression opened a subtree that never closed,
    so example.com's whole body came back empty (found by a real fetch, not by
    a well-formed fixture)."""
    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<link rel=icon href=data:,><meta name=viewport content='width=device-width'>"
        "<title>Example Domain</title><style>body{color:red}</style></head>"
        "<body><div><h1>Example Domain</h1><p>This domain is for examples.</p></div></body></html>"
    )

    out = html_to_text(html)

    assert "Example Domain" in out
    assert "This domain is for examples." in out
    for junk in ("color:red", "width=device-width"):
        assert junk not in out
