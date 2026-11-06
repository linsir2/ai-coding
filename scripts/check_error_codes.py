"""CI gate for the unified tool-error contract (2026-11).

Two invariants that a compiler cannot see:

1. Every failed ``ToolResult(...)`` in ``ai_coding`` carries an ``error`` code,
   and that literal is either one of the 16 gRPC families or a registered
   legacy leaf (``normalize()`` turns it into family + detail).
2. The frontend's ``FAILURES`` table labels exactly the 16 families, so the UI
   cannot silently fall back to ``未知原因`` for a family the backend added.

Run from the repository root:

    python scripts/check_error_codes.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_coding.domain.error_codes import LEGACY_TO_FAMILY, ToolError  # noqa: E402

PACKAGE = ROOT / "ai_coding"
TOOLMETA = PACKAGE / "frontend" / "src" / "util" / "toolmeta.ts"
FAMILIES = {member.value for member in ToolError}


def _error_keyword(call: ast.Call) -> ast.keyword | None:
    return next((kw for kw in call.keywords if kw.arg == "error"), None)


def _success_is_false(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "success":
            return isinstance(kw.value, ast.Constant) and kw.value.value is False
    if call.args:
        first = call.args[0]
        return isinstance(first, ast.Constant) and first.value is False
    return False


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def scan_tool_results() -> list[str]:
    """Failed ToolResults need a registered code; literal codes must exist."""
    problems: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        # The registry itself defines the vocabulary; it has no ToolResult.
        if path.name == "error_codes.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - a broken file fails lint first
            problems.append(f"{path.relative_to(ROOT)}: cannot parse: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "ToolResult":
                continue
            keyword = _error_keyword(node)
            if _success_is_false(node) and keyword is None:
                problems.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: "
                    "ToolResult(success=False) without an error code"
                )
                continue
            if keyword is not None and isinstance(keyword.value, ast.Constant):
                code = keyword.value.value
                if isinstance(code, str) and code not in FAMILIES and code not in LEGACY_TO_FAMILY:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: "
                        f"unregistered error code {code!r}"
                    )
    return problems


def scan_frontend_labels() -> list[str]:
    text = TOOLMETA.read_text(encoding="utf-8")
    block = text.split("const FAILURES:", 1)[1].split("\n}", 1)[0]
    keys = set(re.findall(r"^\s{2}(\w+):", block, flags=re.MULTILINE))
    problems = []
    missing = sorted(FAMILIES - keys)
    extra = sorted(keys - FAMILIES)
    if missing:
        problems.append(f"toolmeta.ts is missing labels: {missing}")
    if extra:
        problems.append(f"toolmeta.ts labels unknown families: {extra}")
    return problems


def main() -> int:
    problems = scan_tool_results() + scan_frontend_labels()
    if problems:
        print("error-code contract violations:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"error-code contract OK: {len(FAMILIES)} families, "
          f"{len(LEGACY_TO_FAMILY)} legacy leaves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
