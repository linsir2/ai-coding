"""Edit tool — precise string replacement with stale-read protection."""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ai_coding.domain.message import ToolArtifact, ToolResult
from ai_coding.infra.file_utils import FileConflict, file_version, write_text_atomic
from ai_coding.security.file_state_tracker import FileState, FileStateTracker
from ai_coding.security.sandbox import (
    Sandbox,
    SandboxViolation,
    open_inside,
    relative_to_root,
)
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx
from ai_coding.tools.stale_read import must_read_message, stale_message


def _nearest_line_hint(text: str, old: str) -> str:
    """Name the closest current line when an exact edit misses.

    Models copy from the read display and can drop punctuation (956c45cb: the
    old string said ``osxosx`` while the file had ``osx-osx``).  A bare "not
    found" sends them back to another whole-file read; the line itself is the
    correction, so show it.  Bounded and advisory: never an automatic match.
    """
    lines = text.splitlines()
    needles = old.splitlines()
    if not lines or not needles:
        return ""
    needle = needles[0].strip()
    best_index = 0
    best_ratio = 0.0
    for index, line in enumerate(lines, start=1):
        ratio = SequenceMatcher(None, line.strip(), needle).ratio()
        if ratio > best_ratio:
            best_index, best_ratio = index, ratio
    if best_ratio < 0.6:
        return ""
    line = lines[best_index - 1]
    if len(line) > 300:
        line = line[:300] + "…"
    return f"\nnearest current line {best_index}: {line}"


def _trim_read_display_indent(old: str) -> str:
    """Undo the two-space display separator models copy from ``read`` output.

    The read tool prints ``<line>  <content>``.  A model that copies a block
    from that display often keeps the separator spaces, so the exact edit misses
    by two columns on every line.  Try the de-indented body only after the exact
    match failed; the caller still requires a unique match.
    """
    if "\n" not in old:
        return ""
    lines = old.splitlines(keepends=True)
    if any(re.match(r"^\s*\d+\s{2}", line) for line in lines):
        return ""
    out: list[str] = []
    changed = False
    for line in lines:
        if line.startswith("  "):
            line = line[2:]
            changed = True
        out.append(line)
    return "".join(out) if changed else ""


class EditTool(BaseTool):
    """Replace a string in a file. ``old_string`` must match exactly once."""

    def __init__(
        self,
        sandbox: Sandbox,
        file_state_tracker: FileStateTracker,
        *,
        plan_root: Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker
        self._plan_root = Path(plan_root) if plan_root is not None else None

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Replace an exact string in a file. The old_string must appear exactly once "
            "unless replace_all is True. You MUST read the file first (stale-read protection). "
            "Edit calls run one at a time and may each require separate approval."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "old_string": {"type": "string", "description": "The exact text to replace."},
                "new_string": {"type": "string", "description": "The replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false).",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    def write_paths_for(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> tuple[str, ...] | None:
        path_str = str(params.get("path", ""))
        if not path_str:
            return None
        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation:
            # ``precheck`` refuses this call before it can run, so there is
            # nothing to serialize.
            return None
        return (str(target),)

    def precheck(self, params: dict[str, Any]) -> ToolResult | None:
        path_str = str(params.get("path", ""))
        if not path_str:
            return None
        try:
            self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")
        return None

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        path_str = str(params.get("path", ""))
        old = str(params.get("old_string", ""))
        new = str(params.get("new_string", ""))
        replace_all = bool(params.get("replace_all", False))

        if not old:
            return ToolResult(
                success=False,
                output="old_string must not be empty",
                error="missing_old_string",
            )

        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")

        if not target.is_file():
            return ToolResult(
                success=False, output=f"file not found: {path_str}", error="file_not_found"
            )

        # Shared tracker: the stale/missing-read verdict is per conversation.
        scope = resolve_ctx(ctx).session_id

        if ctx is not None and ctx.mode == "plan":
            relative = self._plan_relative(path_str)
            if self._plan_root is None or relative is None:
                return ToolResult(
                    success=False,
                    output="计划模式下只能编辑计划目录。",
                    error="plan_directory_required",
                )
            return self._edit_plan_file(
                path_str,
                relative,
                target,
                old,
                new,
                replace_all,
                scope=scope,
            )

        # Stale-read check
        state = self._tracker.check(target, scope=scope)
        if state == FileState.NEVER_READ:
            return ToolResult(
                success=False,
                output=must_read_message("edit", path_str),
                error="must_read_first",
            )
        if state == FileState.STALE:
            return ToolResult(
                success=False,
                output=stale_message("edit", path_str),
                error="stale_read",
            )

        # The version every line below is computed from.  Re-checked right
        # before the atomic rename, so a file that changed between the stale
        # check and this write is refused instead of overwritten.
        before = file_version(target)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                success=False, output=f"read error: {exc}", error="read_error"
            )

        count = text.count(old)
        adjusted = False
        if count == 0:
            candidate = _trim_read_display_indent(old)
            candidate_count = text.count(candidate) if candidate else 0
            if candidate_count == 1:
                old, count, adjusted = candidate, candidate_count, True
        if count == 0:
            return ToolResult(
                success=False,
                output=(
                    f"old_string not found in {path_str}"
                    f"{_nearest_line_hint(text, old)}"
                ),
                error="old_string_not_found",
            )
        if not replace_all and count > 1:
            msg = (
                f"old_string appears {count} times in {path_str}; "
                "it must be unique or set replace_all=true"
            )
            return ToolResult(
                success=False, output=msg, error="old_string_not_unique"
            )

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)

        try:
            write_text_atomic(target, new_text, expected_version=before)
        except FileConflict:
            return ToolResult(
                success=False,
                output=stale_message("edit", path_str),
                error="stale_read",
            )
        except OSError as exc:
            return ToolResult(
                success=False, output=f"write error: {exc}", error="write_error"
            )

        self._tracker.record(target, scope=scope)
        note_effect = resolve_ctx(ctx).note_effect
        if note_effect is not None:
            note_effect(
                self._sandbox.key(target),
                file_version(target),
                "edit",
                "write",
                call_id=resolve_ctx(ctx).call_id,
            )
        replaced = count if replace_all else 1
        return ToolResult(
            success=True,
            output=(
                f"replaced {replaced} occurrence(s) in {path_str}"
                + (" (trimmed read-display indentation)" if adjusted else "")
            ),
            artifacts=(  # r18/F4: see write_tool -- undo replays this list
                ToolArtifact(path=target.relative_to(self._sandbox.workspace).as_posix()),
            ),
        )

    def _plan_relative(self, path_str: str) -> Path | None:
        if self._plan_root is None:
            return None
        candidate = Path(path_str).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self._sandbox.workspace) / candidate
        return relative_to_root(candidate, self._plan_root)

    def _edit_plan_file(
        self,
        path_str: str,
        relative: Path,
        target: Path,
        old: str,
        new: str,
        replace_all: bool,
        *,
        scope: str = "",
    ) -> ToolResult:
        try:
            opened = open_inside(self._plan_root or Path(), relative, mode="readwrite")
        except (SandboxViolation, OSError) as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")
        try:
            # The stale defense is the fstat below on the verified fd, not a
            # path re-stat; target.is_file() above is only the not-found fast
            # path and may misreport under a symlink race.
            pre = os.fstat(opened.fd)
            state = self._tracker.check_stat(
                opened.path, mtime=pre.st_mtime, size=pre.st_size, scope=scope
            )
            if state == FileState.NEVER_READ:
                return ToolResult(
                    success=False,
                    output=must_read_message("edit", path_str),
                    error="must_read_first",
                )
            if state == FileState.STALE:
                return ToolResult(
                    success=False,
                    output=stale_message("edit", path_str),
                    error="stale_read",
                )
            os.lseek(opened.fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(opened.fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
            count = text.count(old)
            adjusted = False
            if count == 0:
                candidate = _trim_read_display_indent(old)
                candidate_count = text.count(candidate) if candidate else 0
                if candidate_count == 1:
                    old, count, adjusted = candidate, candidate_count, True
            if count == 0:
                return ToolResult(
                    success=False,
                    output=f"old_string not found in {path_str}",
                    error="old_string_not_found",
                )
            if not replace_all and count > 1:
                return ToolResult(
                    success=False,
                    output=(
                        f"old_string appears {count} times in {path_str}; "
                        "it must be unique or set replace_all=true"
                    ),
                    error="old_string_not_unique",
                )
            new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            data = new_text.encode("utf-8")
            os.ftruncate(opened.fd, 0)
            os.lseek(opened.fd, 0, os.SEEK_SET)
            while data:
                written = os.write(opened.fd, data)
                data = data[written:]
            post = os.fstat(opened.fd)
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(
                success=False, output=f"edit error: {exc}", error="edit_error"
            )
        finally:
            os.close(opened.fd)

        self._tracker.record_stat(
            opened.path, mtime=post.st_mtime, size=post.st_size, scope=scope
        )
        replaced = count if replace_all else 1
        return ToolResult(
            success=True,
            output=(
                f"replaced {replaced} occurrence(s) in {path_str}"
                + (" (trimmed read-display indentation)" if adjusted else "")
            ),
            artifacts=(
                ToolArtifact(
                    path=opened.path.relative_to(self._sandbox.workspace).as_posix()
                ),
            ),
        )


__all__ = ["EditTool"]
