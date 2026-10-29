"""R20b: the HTML export's syntax highlighter.

The highlighter runs on **tool output**, which is attacker-adjacent: a tool can
be told to ``echo '<script>…'``.  So the tests below pin two things at once —
that each language colours the right tokens, and that nothing it colours can
execute.
"""

from __future__ import annotations

import pytest

from ai_coding.core.export_highlight import escape, guess_language, highlight


def _classes(html: str) -> list[str]:
    """Every ``class`` attribute the highlighter emitted, in order."""
    import re

    return re.findall(r'class="([^"]+)"', html)


# ---------------------------------------------------------------- escaping


def test_a_script_tag_is_escaped_not_executed():
    """The hard constraint.  Tool output is attacker-adjacent."""
    out = highlight("<script>alert(1)</script>", "python")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_an_attribute_breakout_is_escaped():
    """The text *content* may say ``onerror=``; what must not survive is the tag.
    ``<img`` as markup would be a live element."""
    out = highlight('x = "<img onerror=alert(1)>"', "python")
    assert "<img" not in out
    assert "&lt;img" in out
    assert "&gt;" in out


def test_ampersand_and_lt_are_escaped_in_plain_text():
    assert escape("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"


def test_already_safe_text_passes_through_escaping():
    assert escape("hello world") == "hello world"


# ------------------------------------------------------------ language guess


@pytest.mark.parametrize(
    "path,expected",
    [
        ("ai_coding/core/agent_loop.py", "python"),
        ("src/util/slash.ts", "typescript"),
        ("src/app.tsx", "typescript"),
        ("index.js", "javascript"),
        ("package.json", "json"),
        ("README.md", "markdown"),
        ("config.yaml", "yaml"),
        ("pyproject.toml", "toml"),
        ("no-extension", "text"),
    ],
)
def test_language_is_guessed_from_the_suffix(path, expected):
    assert guess_language(path) == expected


def test_bash_tool_output_is_marked_as_shell():
    """The tool name, not the path, decides for shell output."""
    assert guess_language("bash", tool="bash") == "bash"
    assert guess_language("", tool="read") == "text"


# ------------------------------------------------------------------ python


def test_python_comments_are_coloured():
    html = highlight("# a comment\nx = 1", "python")
    assert "tok-com" in _classes(html)
    assert "a comment" in html


def test_python_strings_are_coloured():
    html = highlight('x = "hello"', "python")
    assert "tok-str" in _classes(html)


def test_python_triple_quoted_strings_span_lines():
    html = highlight('s = """line1\nline2"""', "python")
    assert "tok-str" in _classes(html)
    assert "line1" in html and "line2" in html


def test_python_numbers_are_coloured():
    assert "tok-num" in _classes(highlight("x = 42", "python"))


def test_python_keywords_are_coloured():
    html = highlight("def foo():\n    return None", "python")
    classes = _classes(html)
    assert "tok-kw" in classes


def test_python_f_string_prefix_is_part_of_the_string():
    html = highlight('f"value={x}"', "python")
    assert "tok-str" in _classes(html)
    assert "{x}" in html


def test_python_escapes_inside_a_string_are_shown():
    """The backslash-n must survive as text, not become a newline in the HTML."""
    html = highlight('x = "a\\nb"', "python")
    assert "\\n" in html


# -------------------------------------------------------------- typescript


def test_typescript_line_comments_are_coloured():
    assert "tok-com" in _classes(highlight("// note\nconst x = 1", "typescript"))


def test_typescript_block_comments_are_coloured():
    assert "tok-com" in _classes(highlight("/* note */\nconst x = 1", "typescript"))


def test_typescript_template_literals_keep_their_placeholders():
    html = highlight("const s = `a ${b} c`", "typescript")
    assert "tok-str" in _classes(html)
    assert "${b}" in html


def test_typescript_keywords_are_coloured():
    assert "tok-kw" in _classes(highlight("const x: number = 1;", "typescript"))


def test_javascript_is_handled_the_same_way():
    assert "tok-kw" in _classes(highlight("function f() { return 1; }", "javascript"))


# -------------------------------------------------------------------- json


def test_json_keys_are_distinguished_from_strings():
    html = highlight('{"a": "b"}', "json")
    classes = _classes(html)
    assert "tok-key" in classes
    assert "tok-str" in classes


def test_json_literals_are_coloured():
    assert "tok-lit" in _classes(highlight('{"a": true, "b": null}', "json"))


def test_json_numbers_are_coloured():
    assert "tok-num" in _classes(highlight('{"a": 1.5}', "json"))


# ---------------------------------------------------------------- markdown


def test_markdown_headings_are_coloured():
    assert "tok-kw" in _classes(highlight("# Title\n\nbody", "markdown"))


def test_markdown_fenced_code_is_marked_as_a_block():
    html = highlight("```python\nx = 1\n```", "markdown")
    assert "tok-str" in _classes(html) or "tok-com" in _classes(html)


def test_markdown_inline_code_is_coloured():
    assert "tok-str" in _classes(highlight("use `foo()` here", "markdown"))


# -------------------------------------------------------------------- bash


def test_bash_comments_are_coloured():
    assert "tok-com" in _classes(highlight("# note\necho hi", "bash"))


def test_bash_variables_are_coloured():
    assert "tok-num" in _classes(highlight("echo $HOME", "bash")) or "tok-str" in _classes(
        highlight("echo $HOME", "bash")
    )


def test_bash_strings_are_coloured():
    assert "tok-str" in _classes(highlight('echo "hi"', "bash"))


# ---------------------------------------------------------------- yaml/toml


def test_yaml_comments_and_keys_are_coloured():
    html = highlight("# note\nkey: value", "yaml")
    classes = _classes(html)
    assert "tok-com" in classes
    assert "tok-key" in classes


def test_toml_sections_are_coloured():
    assert "tok-key" in _classes(highlight("[section]\nkey = 1", "toml"))


# --------------------------------------------------------------------- diff


def test_diff_added_lines_are_coloured():
    html = highlight("@@ -1 +1 @@\n+added\n-removed", "diff")
    classes = _classes(html)
    assert "tok-add" in classes
    assert "tok-del" in classes


# ------------------------------------------------------------------ fallback


def test_unknown_language_falls_back_to_escaped_plain_text():
    html = highlight("<b>not code</b>", "cobol")
    assert "<b>" not in html
    assert "&lt;b&gt;" in html
    assert _classes(html) == []


def test_empty_input_produces_no_spans():
    assert highlight("", "python") == ""


def test_highlight_never_emits_a_raw_angle_bracket():
    """Sweep: for any input, no ``<`` may appear outside a span tag."""
    import re

    for lang in ("python", "typescript", "json", "markdown", "bash", "yaml", "diff"):
        html = highlight('<script>alert("x")</script>\nfoo(1)', lang)
        stripped = re.sub(r"<span[^>]*>|</span>", "", html)
        assert "<" not in stripped, lang
