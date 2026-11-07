"""Write tool — write/overwrite a file with stale-read protection."""

from __future__ import annotations

import os
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

_DEFAULT_MAX_CHARS = 2_000_000  # 2M chars
#: Above this size, append stops rebuilding the whole file.  Reading and
#: rewriting a multi-megabyte file to add one small chunk is O(size) I/O for an
#: O(1)-sized change; in-place append is the honest tradeoff (with a version
#: recheck and fsync) past this point.
_APPEND_REWRITE_MAX_BYTES = 8 * 1024 * 1024
#: One call's content cap.  Measured (trace 2026-10-31, session f1d881ee):
#: a model asked to write a 30k-char document sends the whole body in one tool
#: call, the provider truncates the arguments, the JSON is invalid, and the
#: model then repeats the same giant call.  A per-call cap plus an append mode
#: makes writing a large file a sequence of small, parseable calls instead.
MAX_CHUNK_CHARS = 8_000


class WriteTool(BaseTool):
    """Write content to a file, creating parent directories as needed.

    Enforces stale-read protection: overwriting an existing file requires that
    the model has previously read it and the file hasn't changed since.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        file_state_tracker: FileStateTracker,
        max_chars: int = _DEFAULT_MAX_CHARS,
        *,
        plan_root: Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._tracker = file_state_tracker
        self._max_chars = max_chars
        self._plan_root = Path(plan_root) if plan_root is not None else None

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file, overwriting if it exists. "
            "Each call writes exactly one file. Multiple write calls in one "
            "message are executed sequentially, never in parallel, and each may "
            "require separate approval. Parent directories are created "
            "automatically. "
            "You MUST read the file first before overwriting it (stale-read protection). "
            f"For a file larger than {MAX_CHUNK_CHARS} characters, write the first "
            "chunk with mode='overwrite' and append the remaining chunks with "
            "mode='append'; never put a whole long document in one call."
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
                "content": {
                    "type": "string",
                    "description": (
                        f"Content to write (at most {MAX_CHUNK_CHARS} characters "
                        "per call; use mode='append' for the rest)."
                    ),
                    "maxLength": MAX_CHUNK_CHARS,
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": (
                        "overwrite (default) replaces the file; append adds to "
                        "the end. Use append for files larger than one chunk."
                    ),
                },
            },
            "required": ["path", "content"],
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

    @staticmethod
    def _mid_file_slice_message(target: Path, content: str) -> str | None:
        """Refuse an overwrite whose body is an exact slice from mid-file.

        The model's body is compared to the current file with surrounding
        whitespace stripped: found at an offset **after** the first character
        and shorter than the old text means "you copied a section and are about
        to replace the whole file with it".  Offset 0 is deliberately allowed:
        a body that is a prefix of the old file is exactly what dropping the
        tail of a file looks like, and that is a legitimate whole-file rewrite.
        """
        try:
            old = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        body = content.strip()
        # Compare against the *stripped* old text: a re-submission of the whole
        # file must not be refused just because the stored file ends with a
        # newline that ``strip`` removed from both sides of the comparison.
        old_body = old.strip()
        if not body or len(body) >= len(old_body):
            return None
        offset = old.find(body)
        # ``offset == 0`` means the body is a prefix (e.g. the model dropped
        # trailing lines), not a mid-file splice; only a strict interior slice
        # is the accident this guard exists for.
        if offset <= 0:
            return None
        line = old.count("\n", 0, offset) + 1
        return (
            f"error: 这次 write 的正文是现有文件中第 {line} 行起的片段，"
            "直接 overwrite 会把整个文件截成这一段（文件未写入）。\n"
            "要改这段就用 edit；要重写整个文件，请先 read 全文，再提交完整内容。"
        )

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        path_str = str(params.get("path", ""))
        content = str(params.get("content", ""))
        mode = str(params.get("mode", "overwrite") or "overwrite").strip().lower()
        if mode not in ("overwrite", "append"):
            return ToolResult(
                success=False,
                output=(
                    f"error: 'mode' must be 'overwrite' or 'append' (got {mode!r})"
                ),
                error="invalid_mode",
            )

        limit = min(self._max_chars, MAX_CHUNK_CHARS)
        if len(content) > limit:
            msg = (
                f"error: 单次写入 {len(content)} 字符超过 {limit} 上限，文件未写入。"
                "请把内容拆成多段：先 write(mode='overwrite') 写第一段，"
                "再用 write(mode='append') 续写；不要重发同一大段内容。"
            )
            return ToolResult(success=False, output=msg, error="content_too_large")

        try:
            target = self._sandbox.resolve(path_str)
        except SandboxViolation as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")
        # ``resolve`` follows symlinks and clamps the result to a root, so every
        # write below targets the real file: ``os.replace`` replaces that file,
        # never the user's symlink itself.

        # The tracker is shared by every session the process serves; the
        # "read it first" verdict must describe *this* conversation.
        scope = resolve_ctx(ctx).session_id

        if ctx is not None and ctx.mode == "plan":
            relative = self._plan_relative(path_str)
            if self._plan_root is None or relative is None:
                return ToolResult(
                    success=False,
                    output="计划模式下只能写入计划目录。",
                    error="plan_directory_required",
                )
            return self._write_plan_file(
                path_str, relative, content, target,
                append=mode == "append", scope=scope,
            )

        # The version the stale check below approves.  Captured before that
        # check and compared again inside ``write_text_atomic`` immediately
        # before the rename, so the check-to-write window is as small as the
        # whole-file read that append now performs (X-01b).
        before = file_version(target)

        # Stale-read check for existing files (append included: adding to a
        # file the model has not read is the same blind edit).
        if target.exists():
            state = self._tracker.check(target, scope=scope)
            if state == FileState.NEVER_READ:
                return ToolResult(
                    success=False,
                    output=must_read_message("write", path_str),
                    error="must_read_first",
                )
            if state == FileState.STALE:
                return ToolResult(
                    success=False,
                    output=stale_message("write", path_str),
                    error="stale_read",
                )

        # 2026-11 real-machine finding: a model used ``mode=overwrite`` with a
        # body that was an exact mid-file slice of the existing file, reducing
        # a 217-line module to 31 lines (pytest then died on
        # "NameError: name 'app' is not defined").  This is not a rewrite; it
        # is an accidental splice.  Exact-slice only, so a genuine (even much
        # shorter) rewrite still passes.
        if mode == "overwrite" and target.exists():
            slice_error = self._mid_file_slice_message(target, content)
            if slice_error:
                return ToolResult(
                    success=False,
                    output=slice_error,
                    error="overwrite_would_truncate",
                )

        large_append = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                size = 0
                if target.exists():
                    try:
                        size = target.stat().st_size
                    except OSError:
                        size = 0
                if size > _APPEND_REWRITE_MAX_BYTES:
                    # Past the cap, rebuilding the file (``old + content``)
                    # would read and rewrite every byte to add a few.  Append in
                    # place instead: re-check the base version immediately before
                    # the first byte, then flush + fsync so a crash can never
                    # leave a buffered half-line behind.  The bytes of a large
                    # file are not decoded here -- doing so is the O(size) read
                    # this branch exists to avoid -- and appending cannot
                    # re-encode or corrupt the existing ones.
                    if file_version(target) != before:
                        raise FileConflict(
                            f"{target} changed while it was being edited"
                        )
                    with target.open("ab") as handle:
                        handle.write(content.encode("utf-8"))
                        handle.flush()
                        os.fsync(handle.fileno())
                    large_append = True
                else:
                    old = ""
                    if target.exists():
                        try:
                            old = target.read_bytes().decode("utf-8")
                        except UnicodeDecodeError:
                            return ToolResult(
                                success=False,
                                output=(
                                    f"error: 无法以 append 模式写入 {path_str}："
                                    "现有文件不是 UTF-8 编码，追加会损坏内容"
                                    "（文件未写入）。请先用 read 确认编码，"
                                    "或改用 overwrite 写入完整的 UTF-8 内容。"
                                ),
                                error="write_error",
                            )
                    write_text_atomic(target, old + content, expected_version=before)
            else:
                write_text_atomic(target, content, expected_version=before)
        except FileConflict:
            return ToolResult(
                success=False,
                output=stale_message("write", path_str),
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
                "write",
                "write",
                call_id=resolve_ctx(ctx).call_id,
            )
        verb = "appended" if mode == "append" else "wrote"
        suffix = " (large-file in-place append)" if large_append else ""
        return ToolResult(
            success=True,
            output=f"{verb} {len(content)} chars to {path_str}{suffix}",
            # r18/F4: the path we actually wrote, normalised to the workspace.
            # Undo needs the list of files a turn created, and this is the only
            # place that knows it -- git cannot tell "the model wrote this" from
            # "the human did, while the turn was running".
            artifacts=(
                ToolArtifact(
                    path=target.relative_to(self._sandbox.workspace).as_posix(),
                    kind="file",
                    bytes=target.stat().st_size,
                ),
            ),
        )

    def _plan_relative(self, path_str: str) -> Path | None:
        if self._plan_root is None:
            return None
        candidate = Path(path_str).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self._sandbox.workspace) / candidate
        return relative_to_root(candidate, self._plan_root)

    def _write_plan_file(
        self,
        path_str: str,
        relative: Path,
        content: str,
        target: Path,
        *,
        append: bool = False,
        scope: str = "",
    ) -> ToolResult:
        # target.exists() is only the NEVER_READ fast path for a brand-new
        # file and may misreport under a symlink race; the authoritative
        # verdict is the fstat below on the verified fd.
        existed = target.exists()
        try:
            # readwrite (not write) so O_TRUNC does not destroy the pre-stat
            # the stale check below needs; truncation happens after the check
            # via ftruncate on the same fd.
            opened = open_inside(
                self._plan_root or Path(),
                relative,
                mode="readwrite",
                create=True,
            )
        except (SandboxViolation, OSError) as exc:
            return ToolResult(success=False, output=str(exc), error="sandbox_violation")
        try:
            pre = os.fstat(opened.fd)
            if existed:
                state = self._tracker.check_stat(
                    opened.path, mtime=pre.st_mtime, size=pre.st_size, scope=scope
                )
                if state == FileState.NEVER_READ:
                    return ToolResult(
                        success=False,
                        output=must_read_message("write", path_str),
                        error="must_read_first",
                    )
                if state == FileState.STALE:
                    return ToolResult(
                        success=False,
                        output=stale_message("write", path_str),
                        error="stale_read",
                    )
            data = content.encode("utf-8")
            if append:
                os.lseek(opened.fd, 0, os.SEEK_END)
            else:
                os.ftruncate(opened.fd, 0)
            while data:
                written = os.write(opened.fd, data)
                data = data[written:]
            post = os.fstat(opened.fd)
            size = post.st_size
        except OSError as exc:
            return ToolResult(
                success=False, output=f"write error: {exc}", error="write_error"
            )
        finally:
            os.close(opened.fd)

        self._tracker.record_stat(
            opened.path, mtime=post.st_mtime, size=post.st_size, scope=scope
        )
        verb = "appended" if append else "wrote"
        return ToolResult(
            success=True,
            output=f"{verb} {len(content)} chars to {path_str}",
            artifacts=(
                ToolArtifact(
                    path=opened.path.relative_to(self._sandbox.workspace).as_posix(),
                    kind="file",
                    bytes=size,
                ),
            ),
        )


__all__ = ["WriteTool"]
