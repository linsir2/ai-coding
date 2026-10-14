"""Grep tool — search file contents (r17/T2).

ripgrep first, pure Python second.  ``rg`` respects .gitignore and is what a
coding agent wants when it is there; a fresh machine without it must still have
grep (this one does not have it — checked), so the fallback is a real path, not
a courtesy.  Both render the same shape, so the model never learns two formats.

OpenCode reaches for the same binary and even downloads it (ripgrep/binary.ts);
downloading is a packaging decision we deliberately do not take here.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.infra.subprocess_runner import run_process_async
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.base import BaseTool, ToolContext

_DEFAULT_MAX_MATCHES = 100
_DEFAULT_TIMEOUT = 30.0
#: Directories a search never walks in the Python fallback.  ``rg`` gets the
#: same effect from .gitignore; parity here is what keeps the two paths'
#: answers comparable (they cannot be byte-identical -- rg has no index).
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".ai-coding"})
_MAX_FILE_BYTES = 1_000_000
_TEXT_SNIFF_BYTES = 4_096
_MAX_LINE_CHARS = 300


class GrepTool(BaseTool):
    """Search file contents by regex or literal string."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        use_ripgrep: bool | None = None,
        runner: Any = run_process_async,
        rg_path: str | None = None,
        max_matches: int = _DEFAULT_MAX_MATCHES,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._sandbox = sandbox
        self._runner = runner
        self._max_matches = max_matches
        self._timeout = timeout
        #: ``None`` = autodetect.  The tests pin either branch explicitly, so
        #: both are exercised on a machine that only has one of them.
        resolved = shutil.which("rg") if use_ripgrep is None else ("rg" if use_ripgrep else None)
        self._rg = rg_path or resolved

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents for a regular expression (or a literal string with "
            "literal=true). Returns 'path:line: text' lines, "
            f"max {self._max_matches} matches."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression (or literal text with literal=true).",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search, relative to workspace.",
                },
                "include": {
                    "type": "string",
                    "description": "Only search files matching this glob (e.g. '*.py').",
                },
                "literal": {
                    "type": "boolean",
                    "description": "Treat pattern as a literal string, not a regex.",
                    "default": False,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Match case-sensitively (default true).",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max matches to return (default {self._max_matches}).",
                },
            },
            "required": ["pattern"],
        }

    # Read-only: safe to run alongside sibling calls in the same message.
    parallel_safe = True

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        pattern = str(params.get("pattern", ""))
        if not pattern:
            return ToolResult(success=False, output="pattern must not be empty")
        target_raw = str(params.get("path", "") or "")
        if ".." in target_raw.replace("\\", "/").split("/"):
            return ToolResult(
                success=False, output="path must not contain '..' (workspace escape blocked)"
            )
        try:
            target = self._sandbox.resolve(target_raw or ".")
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc))

        limit = int(params.get("limit", self._max_matches) or self._max_matches)
        include = str(params.get("include", "") or "")
        literal = bool(params.get("literal", False))
        case_sensitive = bool(params.get("case_sensitive", True))
        try:
            if self._rg:
                return await self._grep_with_rg(
                    target,
                    pattern,
                    include=include,
                    literal=literal,
                    case_sensitive=case_sensitive,
                    limit=limit,
                )
            return self._grep_with_python(
                target,
                pattern,
                include=include,
                literal=literal,
                case_sensitive=case_sensitive,
                limit=limit,
            )
        except re.error as exc:
            return ToolResult(success=False, output=f"invalid pattern: {exc}")
        except TimeoutError:
            return ToolResult(
                success=False, output=f"search timed out after {self._timeout:g} seconds"
            )

    # ---- rendering ---------------------------------------------------------

    def _render(self, lines: list[str], *, total: int) -> ToolResult:
        if not lines:
            return ToolResult(success=True, output="(no matches)")
        output = "\n".join(lines)
        if total > len(lines):
            output += f"\n... [showing {len(lines)} of {total} matches]"
        return ToolResult(success=True, output=output)

    @staticmethod
    def _clip(text: str) -> str:
        text = text.rstrip("\r\n")
        if len(text) > _MAX_LINE_CHARS:
            return text[:_MAX_LINE_CHARS] + "…"
        return text

    # ---- ripgrep -----------------------------------------------------------

    async def _grep_with_rg(
        self, target: Path, pattern: str, *, include: str, literal: bool,
        case_sensitive: bool, limit: int,
    ) -> ToolResult:
        argv: list[str] = [self._rg or "rg", "--json", "--hidden", "--no-messages"]
        if literal:
            argv.append("--fixed-strings")
        if not case_sensitive:
            argv.append("--ignore-case")
        if include:
            argv.append(f"--glob={include}")
        argv += ["--glob=!**/.git/**", "--", pattern, str(target)]
        outcome = await self._runner(argv, cwd=str(self._sandbox.workspace), timeout=self._timeout)
        if outcome.timed_out:
            raise TimeoutError
        parse_error = "regex parse error" in (outcome.stderr or "").lower()
        if outcome.returncode not in (0, 1) and parse_error:
            raise re.error((outcome.stderr or "").strip()[:200])
        lines: list[str] = []
        total = 0
        for raw in (outcome.stdout or "").splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if record.get("type") != "match":
                continue
            total += 1
            if len(lines) >= limit:
                continue
            data = record.get("data", {})
            path = str(data.get("path", {}).get("text", ""))
            number = data.get("line_number", 0)
            text = self._clip(str(data.get("lines", {}).get("text", "")))
            try:
                path = str(Path(path).resolve().relative_to(self._sandbox.workspace))
            except (OSError, ValueError):
                pass
            lines.append(f"{path}:{number}: {text}")
        return self._render(lines, total=total)

    # ---- pure Python fallback ---------------------------------------------

    def _grep_with_python(
        self, target: Path, pattern: str, *, include: str, literal: bool,
        case_sensitive: bool, limit: int,
    ) -> ToolResult:
        flags = 0 if case_sensitive else re.IGNORECASE
        needle = re.escape(pattern) if literal else pattern
        matcher = re.compile(needle, flags)
        files: Sequence[Path] = [target] if target.is_file() else self._walk(target)
        lines: list[str] = []
        total = 0
        for path in files:
            if include and not fnmatch.fnmatch(path.name, include):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:_TEXT_SNIFF_BYTES]:
                continue  # binary
            text = raw.decode("utf-8", errors="replace")
            try:
                rel = str(path.relative_to(self._sandbox.workspace))
            except ValueError:
                rel = str(path)
            for number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line):
                    total += 1
                    if len(lines) < limit:
                        lines.append(f"{rel}:{number}: {self._clip(line)}")
        return self._render(lines, total=total)

    def _walk(self, root: Path) -> list[Path]:
        found: list[Path] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            try:
                path.resolve().relative_to(self._sandbox.workspace)
            except (OSError, ValueError):
                continue
            found.append(path)
        return found


__all__ = ["GrepTool"]
