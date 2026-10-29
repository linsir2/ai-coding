"""Lightweight multi-language syntax highlighting for the HTML export (R20b).

The exporter's whole job is to show **tool output**, and tool output is
attacker-adjacent: a tool can be told to ``echo '<script>…'``.  The rule this
module keeps is therefore unusual and absolute —

    **Tokenize first, escape each token, then wrap it in a span.**

Escaping first and highlighting second would cut HTML entities in half; the
other order means an escaped string can never contain a live tag.  Every test
in ``tests/test_export_highlight.py`` pins that, including a sweep that asserts
no ``<`` survives outside a span tag for any language.

No semantic analysis: keywords, strings, comments and numbers are recognised
lexically, and a name is never distinguished from a function.
"""

from __future__ import annotations

import re
from html import escape as _escape

__all__ = ["escape", "guess_language", "highlight"]


def escape(text: str) -> str:
    """HTML-escape a plain string (no highlighting)."""
    return _escape(text or "")


#: Token classes.  The CSS in ``export_html`` colours them from the TUI's
#: ``theme/tokens.ts`` so the page looks like the product.
COMMENT = "tok-com"
STRING = "tok-str"
NUMBER = "tok-num"
KEYWORD = "tok-kw"
KEY = "tok-key"
LITERAL = "tok-lit"
ADDED = "tok-add"
REMOVED = "tok-del"


# ------------------------------------------------------------- language guess

_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "bash",
    ".bash": "bash",
    ".diff": "diff",
    ".patch": "diff",
}

_TOOLS = {"bash": "bash", "grep": "text", "glob": "text", "read": "text"}


def guess_language(path: str, *, tool: str = "") -> str:
    """Which highlighter to use for a piece of output.

    The tool name decides for output that has no path of its own: ``bash`` output
    is shell, while ``read`` output is whatever file it read (so it falls back to
    the caller's path, or plain text).
    """
    if tool in _TOOLS and _TOOLS[tool] != "text":
        return _TOOLS[tool]
    name = (path or "").strip().replace("\\", "/").lower()
    for suffix, language in _BY_SUFFIX.items():
        if name.endswith(suffix):
            return language
    return "text"


# --------------------------------------------------------------- token rules

#: One rule is (class, compiled regex).  The scanner walks the string left to
#: right, tries each rule in order at the current position, and emits the first
#: that matches.  Order matters: a triple-quoted string must be tried before a
#: single-quoted one, and ``//`` before a number.
_PY_KW = (
    r"\b(?:False|None|True|and|as|assert|async|await|break|class|continue|def|del|"
    r"elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|"
    r"or|pass|raise|return|try|while|with|yield|match|case|print|len|range|self)\b"
)
_TS_KW = (
    r"\b(?:break|case|catch|class|const|continue|debugger|default|delete|do|else|"
    r"export|extends|finally|for|from|function|if|import|in|instanceof|let|new|of|"
    r"return|static|super|switch|this|throw|try|typeof|var|void|while|with|yield|"
    r"async|await|interface|type|enum|namespace|implements|private|public|protected|"
    r"readonly|abstract|as|keyof|is|infer|declare|module|require|true|false|null|"
    r"undefined|number|string|boolean|any|unknown|never|object)\b"
)

_RULES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "python": (
        (COMMENT, re.compile(r"#[^\n]*")),
        (STRING, re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')),
        (STRING, re.compile(r'f?"(?:\\.|[^"\\\n])*"|f?\'(?:\\.|[^\'\\\n])*\'')),
        (NUMBER, re.compile(r"\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b")),
        (KEYWORD, re.compile(_PY_KW)),
        (LITERAL, re.compile(r"@\w+")),
    ),
    "typescript": (
        (COMMENT, re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")),
        (STRING, re.compile(r"`(?:\\.|[^`\\])*`")),
        (STRING, re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')),
        (NUMBER, re.compile(r"\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b")),
        (KEYWORD, re.compile(_TS_KW)),
    ),
    "javascript": (
        (COMMENT, re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")),
        (STRING, re.compile(r"`(?:\\.|[^`\\])*`")),
        (STRING, re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')),
        (NUMBER, re.compile(r"\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b")),
        (KEYWORD, re.compile(_TS_KW)),
    ),
    "json": (
        (KEY, re.compile(r'"(?:\\.|[^"\\])*"(?=\s*:)')),
        (STRING, re.compile(r'"(?:\\.|[^"\\])*"')),
        (LITERAL, re.compile(r"\b(?:true|false|null)\b")),
        (NUMBER, re.compile(r"-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")),
    ),
    "markdown": (
        (KEYWORD, re.compile(r"(?m)^#{1,6}[^\n]*")),
        (STRING, re.compile(r"```[\s\S]*?```")),
        (STRING, re.compile(r"`[^`\n]*`")),
        (KEYWORD, re.compile(r"(?m)^(?:[-*+]|\d+\.)\s")),
    ),
    "bash": (
        (COMMENT, re.compile(r"#[^\n]*")),
        (STRING, re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')),
        (NUMBER, re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")),
        (KEYWORD, re.compile(r"\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|"
                             r"function|return|echo|export|local|in)\b")),
    ),
    "yaml": (
        (COMMENT, re.compile(r"#[^\n]*")),
        (KEY, re.compile(r"(?m)^[\s-]*[A-Za-z_][\w.-]*(?=\s*:)")),
        (STRING, re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')),
        (LITERAL, re.compile(r"\b(?:true|false|null|yes|no)\b")),
        (NUMBER, re.compile(r"\b\d+(?:\.\d+)?\b")),
    ),
    "toml": (
        (COMMENT, re.compile(r"#[^\n]*")),
        (KEY, re.compile(r"(?m)^\s*(?:\[[^\]]+\]|[A-Za-z_][\w.-]*(?=\s*=))")),
        (STRING, re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')),
        (LITERAL, re.compile(r"\b(?:true|false)\b")),
        (NUMBER, re.compile(r"\b\d+(?:\.\d+)?\b")),
    ),
    "diff": (
        (ADDED, re.compile(r"(?m)^\+[^+][^\n]*")),
        (REMOVED, re.compile(r"(?m)^-[^-][^\n]*")),
        (COMMENT, re.compile(r"(?m)^@@[^\n]*")),
    ),
}


def _highlight_with(
    text: str, rules: tuple[tuple[str, re.Pattern[str]], ...]
) -> str:
    out: list[str] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        for css_class, pattern in rules:
            match = pattern.match(text, cursor)
            if match is None:
                continue
            out.append(f'<span class="{css_class}">{_escape(match.group(0))}</span>')
            cursor = match.end()
            break
        else:
            # No rule matched this character: emit it escaped and advance one.
            # Grouping the plain runs would save spans; the tests pin the
            # behaviour, and the export's size is bounded by the trace.
            out.append(_escape(text[cursor]))
            cursor += 1
    return "".join(out)


def highlight(code: str, language: str) -> str:
    """Render ``code`` as highlighted, escaped HTML.

    An unknown language (or ``text``) is escaped and returned plain: a wrong
    colour is a cosmetic bug, a live tag is a security bug.
    """
    if not code:
        return ""
    rules = _RULES.get(language)
    if rules is None:
        return _escape(code)
    return _highlight_with(code, rules)
