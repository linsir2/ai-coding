"""Read tool — read a text file with line numbers and optional offset/limit."""

from __future__ import annotations

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
        if (
            is_credential_path(path_str, workspace=workspace)
            or is_credential_path(target)
        ) and not approved:
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

        try:
            size = target.stat().st_size
        except OSError as exc:
            return ToolResult(success=False, output=f"cannot stat file: {exc}")

        if size > self._max_file_size:
            msg = f"file too large ({size} bytes > {self._max_file_size} limit)"
            return ToolResult(success=False, output=msg)

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(success=False, output=f"read error: {exc}")

        lines = text.splitlines(keepends=False)
        # Clamp twice: ``offset + limit`` runs past the end of the file, and the
        # ledger must never record a range wider than the file it describes.
        end = min(offset + limit, len(lines)) if limit is not None else len(lines)
        end = max(end, offset)
        page_end = min(end, offset + self._max_lines)
        view = lines[offset:page_end]

        # line-numbered output, right-aligned 6-wide like `cat -n`
        numbered = [f"{i + 1:>6}  {line}" for i, line in enumerate(view, start=offset)]
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
        # The ledger records the *returned* page, so a paged read of a 3,081-line
        # file accumulates instead of pretending it read the whole thing.
        note_read = resolve_ctx(ctx).note_read
        credential = is_credential_path(path_str, workspace=workspace) or is_credential_path(target)
        if note_read is not None and not credential:
            note_read(path_str, offset + 1, page_end, len(lines))

        return ToolResult(success=True, output=output)


__all__ = ["ReadTool"]
