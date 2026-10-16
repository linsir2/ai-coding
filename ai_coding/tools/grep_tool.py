"""Grep tool — search file contents (r17/T2).

ripgrep first, pure Python second.  ``rg`` respects .gitignore and is what a
coding agent wants when it is there; a fresh machine without it must still have
grep (this one does not have it — checked), so the fallback is a real path, not
a courtesy.  Both render the same shape, so the model never learns two formats.

OpenCode reaches for the same binary and even downloads it (ripgrep/binary.ts);
downloading is a packaging decision we deliberately do not take here.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ai_coding.core.workspace_files import SKIP_DIRS, walk_workspace
from ai_coding.domain.message import ToolResult
from ai_coding.infra.subprocess_runner import run_process_async
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.base import BaseTool, ToolContext

_DEFAULT_MAX_MATCHES = 100
_DEFAULT_TIMEOUT = 30.0
_MAX_FILE_BYTES = 1_000_000
_TEXT_SNIFF_BYTES = 4_096
_MAX_LINE_CHARS = 300


def _include_matches(rel: PurePosixPath, include: str) -> bool:
    """Whether ``include`` selects ``rel`` -- ripgrep's rule, not fnmatch's.

    Measured against rg 15.1.0: a pattern with no ``/`` matches the basename at
    any depth, a pattern with ``/`` matches the path relative to the search
    root, and a leading ``**/`` means "any depth including none" -- which
    ``PurePath.match`` does not do, since it reads ``**`` as one segment.
    """
    pattern = include[3:] if include.startswith("**/") else include
    return rel.match(pattern)


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
    mutates_world = False

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
            # The fallback walks and reads the tree; on a real repository that is
            # seconds of blocking work.  read_tool does the same thing in a thread
            # (r18/F9) and the SSE bridge has to keep ticking while it runs.
            return await asyncio.to_thread(
                self._grep_with_python,
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

    def _render(
        self, hits: list[tuple[str, int, str]], limit: int, *, total: int | None = None
    ) -> ToolResult:
        """The one place hits become output (r18/F10).

        Both runners hand over ``(path, line, text)`` rows; sorting and
        truncating happen here, once.  ripgrep emits rows in parallel-walk
        completion order, so before this the two paths cut the list at
        different rows and reported a ``total`` that described neither.
        """
        total = len(hits) if total is None else total
        rows = sorted(hits)[:limit]
        if not rows:
            return ToolResult(success=True, output="(no matches)")
        output = "\n".join(f"{path}:{number}: {text}" for path, number, text in rows)
        if total > len(rows):
            output += f"\n... [showing {len(rows)} of {total} matches]"
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
        argv: list[str] = [
            self._rg or "rg", "--json", "--hidden", "--no-messages",
            # r18/F10: rg follows .gitignore only inside a repository; we follow
            # it everywhere, so tell it not to insist on a repo.
            "--no-require-git",
            # r20/F6: the fallback refuses a file over _MAX_FILE_BYTES and rg
            # did not, so the same question got two answers depending on whether
            # rg was installed.  grep searches; reading one big file is
            # read_tool's job, and that cap is 10 MB.
            f"--max-filesize={_MAX_FILE_BYTES}",
        ]
        if literal:
            argv.append("--fixed-strings")
        if not case_sensitive:
            argv.append("--ignore-case")
        if include:
            argv.append(f"--glob={include}")
        # The same directories the fallback prunes.  rg has no built-in ignore,
        # so without these the two runners disagreed about node_modules (F15).
        argv += [f"--glob=!**/{name}/**" for name in sorted(SKIP_DIRS)]
        argv += ["--", pattern, str(target)]
        outcome = await self._runner(argv, cwd=str(self._sandbox.workspace), timeout=self._timeout)
        if outcome.timed_out:
            raise TimeoutError
        # A runner that never started (returncode None + error) and an exit code
        # outside 0/1 are both failures.  Reading them as "no hits" is the worst
        # outcome this tool can produce: the model keeps reasoning from a false
        # premise (r18/F9).  rg exits 0 on a hit, 1 on none, 2 on an error.
        if outcome.error or outcome.returncode is None:
            return ToolResult(
                success=False, output=f"grep runner failed: {outcome.error or 'no exit status'}"
            )
        parse_error = "regex parse error" in (outcome.stderr or "").lower()
        if outcome.returncode not in (0, 1):
            if parse_error:
                raise re.error((outcome.stderr or "").strip()[:200])
            return ToolResult(
                success=False,
                output=f"grep failed (rg exit {outcome.returncode}): "
                f"{(outcome.stderr or '').strip()[:200]}",
            )
        hits: list[tuple[str, int, str]] = []
        for raw in (outcome.stdout or "").splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if record.get("type") != "match":
                continue
            data = record.get("data", {})
            # rg runs with cwd=workspace, so its paths are already relative to
            # it.  Resolving them here used the *server's* cwd, which only came
            # out right because the resulting ValueError left the raw string
            # in place (r19/F1).
            path = self._workspace_relative(target, data.get("path", {}).get("text", ""))
            hits.append(
                (
                    path.as_posix(),
                    int(data.get("line_number", 0)),
                    self._clip(str(data.get("lines", {}).get("text", ""))),
                )
            )
        return self._render(hits, limit)

    # ---- pure Python fallback ---------------------------------------------

    def _grep_with_python(
        self, target: Path, pattern: str, *, include: str, literal: bool,
        case_sensitive: bool, limit: int,
    ) -> ToolResult:
        flags = 0 if case_sensitive else re.IGNORECASE
        needle = re.escape(pattern) if literal else pattern
        matcher = re.compile(needle, flags)
        files: Sequence[tuple[Path, PurePosixPath]] = (
            [(target, PurePosixPath(target.name))]
            if target.is_file()
            else self._walk(target)
        )
        anchor = PurePosixPath(target.relative_to(self._sandbox.workspace).as_posix())
        hits: list[tuple[str, int, str]] = []
        for path, rel in files:
            shown = rel if anchor == PurePosixPath(".") else anchor / rel
            if include and not _include_matches(shown, include):
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
            for number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line):
                    hits.append((shown.as_posix(), number, self._clip(line)))
        return self._render(hits, limit)

    def _walk(self, root: Path) -> list[tuple[Path, PurePosixPath]]:
        """The files under ``root``, through the shared workspace walk.

        ``walk_workspace`` yields paths relative to *its* root, so they are
        read from there; what the model is shown is relative to the workspace,
        which is the only frame it can act on.  Joining the target-relative
        path onto the workspace instead was invisible until a search was
        actually pointed at a subdirectory (r19/F1).
        """
        return [(root / rel, rel) for rel, is_dir in walk_workspace(root) if not is_dir]

    def _workspace_relative(self, target: Path, from_target: str) -> PurePosixPath:
        """Re-anchor a path the search root produced onto the workspace.

        ripgrep echoes the search argument it was handed, and we always hand it
        an absolute one, so its matches come back absolute.  ``PurePosixPath``
        returns an absolute right operand unchanged, so joining first let
        ``/home/me/ws/sub/c.txt`` straight through -- the model saw a
        machine-specific path while the Python fallback, searching the same
        file, said ``sub/c.txt``.  Strip the absolute form back to ``target``
        first, then anchor; a relative result is already workspace-relative
        because the runner's cwd is the workspace.
        """
        tail = PurePosixPath(from_target)
        prefix = PurePosixPath(target.relative_to(self._sandbox.workspace).as_posix())
        if tail.is_absolute():
            tail = tail.relative_to(PurePosixPath(target.as_posix()))
        return tail if prefix == PurePosixPath(".") else prefix / tail


__all__ = ["GrepTool"]
