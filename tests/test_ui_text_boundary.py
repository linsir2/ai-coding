"""Architecture guard: untrusted text must never enter a markup-parsing API.

Textual and rich parse ``str`` content as console markup by default, so a ``[``
in text the app does not own either *vanishes* (``build [k=v]`` → ``build ``)
or aborts the whole app (``MarkupError: Expected markup value`` on
``[path=/tmp/x]``).  Every ``Static``, ``Option`` and ``Console.print`` is such
a door, and the first two cannot be closed at the call site (a widget's
``markup=False`` only covers its own text; ``Option.prompt`` takes data, not
options).  So the boundary lives in the data: ``ai_coding.ui.plain``.

Audited channels — keep this list in sync when a new one appears, and extend the
rules below with it:

===========================  ==========================================
Channel                      Rule
===========================  ==========================================
``Static`` / ``Label``       G1: use ``plain.Plain`` (markup off)
``Option`` / ``Hit``         G2: display arg via ``plain()``/``Text()``
``Console.print``            G3: pass ``markup=False``
``Collapsible(title=…)``     safe: Textual wraps it with ``Content.from_text``
``Prose`` (rich.markdown)    safe: markdown is the intent, markup off
``RichLog.write``            safe: retired from product code (asserted below)
``Input`` placeholder/label  safe: app-owned literals only
===========================  ==========================================
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import ai_coding

PKG = Path(ai_coding.__file__).parent
PLAIN_MODULE = PKG / "ui" / "plain.py"

#: Callees allowed to *produce* a display value for an option row.
ALLOWED_DISPLAY_BUILDERS = {"plain", "Text", "Content", "highlight"}


def _sources() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _callee_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _display_arg_index(name: str) -> int:
    """``DiscoveryHit(display, …)`` vs ``Hit(score, match_display, …)``."""
    return 1 if name == "Hit" else 0


def _is_safe_display(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):  # a literal in our own source
        return True
    if isinstance(node, ast.Call):
        return _callee_name(node.func) in ALLOWED_DISPLAY_BUILDERS
    return False


def _violations(call: ast.Call, path: Path) -> str | None:
    name = _callee_name(call.func)
    if name == "Static" and path != PLAIN_MODULE:
        return f"{path.name}:{call.lineno}: bare Static(...) — use plain.Plain"
    if name in {"Option", "DiscoveryHit", "Hit"}:
        args = call.args
        index = _display_arg_index(name)
        if len(args) > index and not _is_safe_display(args[index]):
            return (
                f"{path.name}:{call.lineno}: {name}() display arg is not literal/"
                f"plain()/Text() — untrusted text reaches markup parsing"
            )
    return None


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_untrusted_text_reaches_a_markup_api(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = [
        message
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for message in [_violations(node, path)]
        if message
    ]
    assert not problems, "\n".join(problems)


def test_console_prints_disable_markup():
    """``rich.Console.print`` parses markup unless told not to (G3)."""
    path = PKG / "ui" / "console.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prints = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_name(node.func) == "print"
    ]
    assert prints, "console.py no longer prints — revisit this guard"
    unsafe = [
        f"console.py:{node.lineno}"
        for node in prints
        if not any(
            kw.arg == "markup" and isinstance(kw.value, ast.Constant) and kw.value.value is False
            for kw in node.keywords
        )
    ]
    assert not unsafe, f"rich print without markup=False: {unsafe}"


def test_rich_log_is_not_used_by_product_code():
    """``RichLog.write`` is a markup channel too; product code must not use it."""
    offenders = [
        f"{path.relative_to(PKG)}"
        for path in _sources()
        if "RichLog" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"RichLog reintroduced: {offenders}"

# ---- behaviour: the two channels a guard cannot prove on its own --------------


@pytest.mark.asyncio
async def test_resume_picker_row_survives_brackets_in_preview():
    """`/resume` shows a session's own first message — user text, brackets included."""
    from textual.app import App, ComposeResult
    from textual.widgets import OptionList

    from ai_coding.ui.resume_screen import ResumeScreen

    preview = "[INFO] [path=/tmp/x] run build [k=v]"
    session = SimpleNamespace(
        session_id="0123456789ab" * 3,
        first_preview=preview,
        last_access_time="2026-09-30T10:00:00Z",
        message_count=5,
    )

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            from textual.widgets import Static

            yield Static("host")

    async with _Host().run_test(size=(80, 10)) as pilot:
        await pilot.app.push_screen(ResumeScreen([session]))
        for _ in range(4):
            await pilot.pause()
        options = pilot.app.screen.query_one(OptionList)
        shown = getattr(options.get_option_at_index(1).prompt, "plain", "")
        assert "[path=/tmp/x]" in shown
        assert "build [k=v]" in shown


def test_dialogue_console_prints_brackets_verbatim():
    from rich.console import Console

    from ai_coding.app.console import DialogueConsole

    recorder = Console(record=True, width=200)
    console = DialogueConsole(recorder)
    console.info("[INFO] build [k=v] done")
    console.error("[path=/tmp/x] failed")
    printed = recorder.export_text()
    assert "[INFO] build [k=v] done" in printed
    assert "[path=/tmp/x] failed" in printed
