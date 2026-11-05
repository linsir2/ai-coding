"""Read tool — read a text file with line numbers and optional offset/limit."""

from __future__ import annotations

import os
from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.security.credentials import is_credential_path
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

_DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

#: R20/RC-3: the per-call line cap.  Measured on T2: one whole-file read of
#: ``agent_loop.py`` was 174,113 characters (≈43k tokens, 89% of the 48k window)
#: and the tail budget is 12k tokens, so it either overflowed or got summarised
#: down to a 2,000-character excerpt -- the content was gone either way.  500
#: lines is ~20k characters (≈5k tokens): it fits the tail, and a 441-line file
#: now arrives in one call instead of eleven.
_DEFAULT_MAX_LINES = 500


class ReadTool(BaseTool):
    """Read a text file with line numbers; supports offset/limit pagination."""

    def __init__(
        self,
        sandbox: Sandbox,
        file_state_tracker: FileStateTracker,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
        max_lines: int = _DEFAULT_MAX_LINES,
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker
        self._max_file_size = max_file_size
        self._max_lines = max_lines

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a text file with line numbers. "
            f"Returns at most {self._max_lines} lines per call; the result says "
            "where to continue. Files that fit are returned whole. "
            f"Max file size: {self._max_file_size} bytes."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (relative to workspace).",
                },
                "offset": {
                    "type": "integer",
                    "description": "0-based start line (default 0).",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max lines to read (default {self._max_lines}; the tool "
                        "never returns more than that and says where to continue)."
                    ),
                },
            },
            "required": ["path"],
        }

    # Read-only: safe to run alongside sibling calls in the same message.
    parallel_safe = True
    mutates_world = False

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
        offset = int(params.get("offset", 0) or 0)
        limit = params.get("limit")
        limit = int(limit) if limit is not None else None

        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")

        approved = resolve_ctx(ctx).credential_approved
        workspace = getattr(self._sandbox, "workspace", None)
        credential = (
            is_credential_path(path_str, workspace=workspace)
            or is_credential_path(target)
        )
        if credential and not approved:
            return ToolResult(
                success=False,
                output=(
                    f"reading credential file '{path_str}' requires an explicit "
                    "human approval for this call"
                ),
                error="credential_approval_required",
            )

        if not target.is_file():
            return ToolResult(success=False, output=f"file not found: {path_str}")

        # Read and stat the same descriptor.  The old read-then-stat order
        # could pair old bytes with a newer version when a writer raced us,
        # which would make the coverage guard deny a necessary refresh.
        try:
            with target.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                if stat.st_size > self._max_file_size:
                    msg = (
                        f"file too large ({stat.st_size} bytes > "
                        f"{self._max_file_size} limit)"
                    )
                    return ToolResult(success=False, output=msg)
                raw = handle.read()
                post = os.fstat(handle.fileno())
        except OSError as exc:
            return ToolResult(success=False, output=f"read error: {exc}")
        if (
            stat.st_mtime_ns,
            stat.st_size,
            stat.st_ino,
        ) != (post.st_mtime_ns, post.st_size, post.st_ino):
            return ToolResult(
                success=False,
                output=(
                    f"file changed while it was being read: {path_str}; "
                    "retry the read"
                ),
                error="read_raced",
            )

        text = raw.decode("utf-8", errors="replace")
        version = f"{post.st_mtime_ns}:{post.st_size}:{post.st_ino}"

        lines = text.splitlines(keepends=False)
        # Clamp twice: ``offset + limit`` runs past the end of the file, and the
        # ledger must never record a range wider than the file it describes.
        end = min(offset + limit, len(lines)) if limit is not None else len(lines)
        end = max(end, offset)
        page_end = min(end, offset + self._max_lines)

        resolved_ctx = resolve_ctx(ctx)
        key = self._sandbox.key(target)
        # R2-13: a re-read of lines this session already returned is not new
        # information, and sending them again is what let a model read its own
        # growing trace until the turn clock ran out (22 retrievals, 262k
        # prompt tokens, one 300s timeout).  Only the *uncovered* lines travel;
        # a fully covered request answers with a pointer.
        marks = resolved_ctx.read_marks(key) if resolved_ctx.read_marks else ()
        gaps = _uncovered(offset + 1, page_end, marks) if marks else [(offset + 1, page_end)]
        repeated = bool(marks) and gaps != [(offset + 1, page_end)]
        changed = bool(marks) and any(mark_version != version for _, _, mark_version in marks)

        if not gaps:
            # Nothing new: say which lines were already in the context and how
            # to ask for them again.  A silent empty answer would look broken.
            asked = limit if limit is not None else page_end - offset
            again = f"offset={offset} " if offset else ""
            hint = "文件已变化；若已读行有改动，请带 offset 重读" if changed else "文件未变化"
            return ToolResult(
                success=True,
                output=(
                    f"[未返回正文：第 {offset + 1}-{page_end} 行在本会话已经读过"
                    f"（共请求 {asked} 行，{hint}）。需要原样再看一遍请用 "
                    f"{again}limit={asked} 重新指定；需要新内容请读未覆盖的行。]"
                ),
                data={"repeated_read": True, "changed": changed},
            )

        view_numbers = {
            number
            for start, stop in gaps
            for number in range(start, stop + 1)
        }
        # line-numbered output, right-aligned 6-wide like `cat -n`
        numbered = [
            f"{number:>6}  {line}"
            for number, line in enumerate(lines, start=1)
            if number in view_numbers
        ]
        output = "\n".join(numbered)
        # Two different truncations, two different messages.  The cap is the
        # harness's doing and the model must be told where to resume; an explicit
        # ``limit`` is the model's own choice, and the old "showing lines" suffix
        # already said everything it needs to.
        capped = page_end < len(lines) and page_end - offset >= self._max_lines
        if page_end <= offset:
            # Nothing came back: ``limit=0``, a negative limit, or an offset past
            # the end of the file.  All three are the model's input being wrong,
            # and the only honest answer is to say so -- "showing lines 1-0 of
            # 100" is a contradiction the model would have to guess at.
            asked = limit if limit is not None else 0
            output += (
                f"\n[没有返回任何行：请求第 {offset + 1} 行起共 {asked} 行，"
                f"文件共 {len(lines)} 行]"
            )
        elif repeated:
            skipped = (page_end - offset) - len(view_numbers)
            where = "文件已变化" if changed else "文件未变化"
            output += (
                f"\n[只返回未读过的行：{where}，跳过本会话已读过的 {skipped} 行；"
                "已读行若有改动请带 offset 重读]"
            )
        elif capped:
            output += (
                f"\n[已返回第 {offset + 1}-{page_end} 行（共 {len(lines)} 行），"
                f"用 offset={page_end} 继续]"
            )
        elif offset > 0 or (limit is not None and end < len(lines)):
            output += f"\n[showing lines {offset + 1}-{page_end} of {len(lines)}]"

        # record snapshot for stale-read protection
        self._tracker.record(target)

        # R20/RC-2: tell the harness which lines actually reached the context.
        # The ledger records the *returned* runs, so a paged read of a 3,081-line
        # file accumulates instead of pretending it read the whole thing -- and a
        # skipped middle is not recorded as read.
        note_read = resolved_ctx.note_read
        if note_read is not None and not credential:
            for start, stop in gaps:
                note_read(
                    key,
                    start,
                    stop,
                    len(lines),
                    version,
                    call_id=resolved_ctx.call_id,
                    canonical=True,
                )

        return ToolResult(
            success=True,
            output=output,
            data={"repeated_read": repeated, "changed": changed},
        )


__all__ = ["ReadTool"]


def _uncovered(
    start: int, end: int, marks: tuple[tuple[int, int, str], ...]
) -> list[tuple[int, int]]:
    """The 1-based inclusive runs of ``start..end`` no mark has covered yet.

    Merging (rather than subtracting mark by mark) is what keeps a set of
    adjacent pages from producing a hundred one-line gaps.  Version is
    deliberately *not* part of the key: the caller reports a version change
    separately, because an append-only file (a trace being written while the
    model reads it) has stable prefix lines and only a growing tail.
    """
    if end < start:
        return []
    covered = sorted(
        (max(mark_start, start), min(mark_end, end))
        for mark_start, mark_end, _ in marks
        if mark_end >= start and mark_start <= end
    )
    merged: list[tuple[int, int]] = []
    for first, last in covered:
        if first > last:
            continue
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    gaps: list[tuple[int, int]] = []
    cursor = start
    for first, last in merged:
        if cursor < first:
            gaps.append((cursor, first - 1))
        cursor = max(cursor, last + 1)
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps
