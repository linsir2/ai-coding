"""Architecture guard: untrusted text must never reach a markup-parsing API.

``rich.Console.print`` parses ``str`` content as console markup by default, so
a ``[`` in text the app does not own either *vanishes* (``build [k=v]`` →
``build ``) or aborts the call (``MarkupError: Expected markup value`` on
``[path=/tmp/x]``).  ``DialogueConsole`` is the only product-code printer, so
the guard is exact: every print in ``app/console.py`` must pass
``markup=False``.

Audited channels — keep this list in sync when a new one appears:

===========================  ==========================================
Channel                      Rule
===========================  ==========================================
``Console.print``            G3: pass ``markup=False``
``RichLog.write``            forbidden: RichLog is not used by product code
===========================  ==========================================
"""

from __future__ import annotations

import ast
from pathlib import Path

import ai_coding

PKG = Path(ai_coding.__file__).parent
CONSOLE_MODULE = PKG / "app" / "console.py"


def _sources() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def test_console_prints_disable_markup():
    """``rich.Console.print`` parses markup unless told not to (G3)."""
    tree = ast.parse(CONSOLE_MODULE.read_text(encoding="utf-8"))
    prints = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "print"
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
        str(path.relative_to(PKG))
        for path in _sources()
        if "RichLog" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"RichLog reintroduced: {offenders}"


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
