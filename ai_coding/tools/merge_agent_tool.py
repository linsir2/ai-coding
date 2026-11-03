"""Explicit merge tool (P2-C3).

The tool is a thin adapter: it validates the model-facing parameters, delegates
to ``Scheduler.merge`` and turns the structured outcome into a ``ToolResult``.
It deliberately holds no queue, worktree or git logic.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext

__all__ = ["MergeAgentTool"]

_SUCCESS = {"merged", "merge_skipped"}


class MergeAgentTool(BaseTool):
    """Ask the one writer to merge a settled child branch into the main tree."""

    def __init__(self, scheduler: Any) -> None:
        self._scheduler = scheduler
        self.parallel_safe = False
        self.mutates_world = True
        self.occupies_slot = False

    @property
    def name(self) -> str:
        return "merge_agent"

    @property
    def description(self) -> str:
        return (
            "Merge one settled sub-agent branch into the main working tree. "
            "This is explicit: nothing is merged automatically. The target may "
            "be a request id, a run id (or prefix), a sub-agent name, or a "
            "branch. An optional verify_cmd runs only inside a temporary "
            "candidate worktree; a non-zero exit leaves the main tree "
            "untouched. Omit verify_cmd to merge without verification; the "
            "result then says it was unverified."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "The settled child to merge: request id, run id prefix, "
                        "unique name, or branch."
                    ),
                },
                "verify_cmd": {
                    "type": "string",
                    "description": (
                        "Optional shell command to run in the temporary merge "
                        "candidate. It must pass for the main tree to change. "
                        "It is judged by the bash permission rules."
                    ),
                },
            },
            "required": ["target"],
        }

    @property
    def max_duration_seconds(self) -> float:
        # verify (120s) + candidate/landing git calls + claim waits, with a
        # margin; the dispatcher still takes the tighter turn deadline.
        return 300.0

    def write_paths_for(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> tuple[str, ...] | None:
        # The MergeQueue takes the workspace claim itself, right before landing.
        # If the dispatcher took it first, the two owners would deadlock.
        return None

    def precheck(self, params: dict[str, Any]) -> ToolResult | None:
        target = params.get("target")
        if not isinstance(target, str) or not target.strip():
            return ToolResult(
                success=False,
                output="error: target is required",
                error="invalid_params",
            )
        verify = params.get("verify_cmd")
        if verify is not None and not isinstance(verify, str):
            return ToolResult(
                success=False,
                output="error: verify_cmd must be a string",
                error="invalid_params",
            )
        return None

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        target = str(params.get("target") or "").strip()
        if not target:
            return ToolResult(
                success=False, output="error: target is required",
                error="invalid_params",
            )
        verify = params.get("verify_cmd")
        if verify is not None and not isinstance(verify, str):
            return ToolResult(
                success=False, output="error: verify_cmd must be a string",
                error="invalid_params",
            )
        verify_cmd = (verify or "").strip() or None
        if self._scheduler is None:
            return ToolResult(
                success=False,
                output="error: merge queue is not available in this session",
                error="unavailable",
            )
        outcome = await self._scheduler.merge(
            target,
            verify_cmd,
            session_id=getattr(ctx, "session_id", "") if ctx is not None else "",
        )
        status = str(outcome.get("status") or "merge_failed")
        success = status in _SUCCESS
        data = {
            "status": status,
            "branch": outcome.get("branch"),
            "base_commit": outcome.get("base_commit"),
            "commit": outcome.get("commit"),
            "main_head": outcome.get("main_head"),
            "conflicting_paths": list(outcome.get("conflicting_paths") or ()),
            "verify": outcome.get("verify"),
            "verified": outcome.get("verified"),
            "error": outcome.get("error"),
            "attempts": outcome.get("attempts"),
            "worktree_path": outcome.get("worktree_path"),
        }
        if outcome.get("cleanup_error"):
            data["cleanup_error"] = outcome["cleanup_error"]
        if outcome.get("journal_error"):
            data["journal_error"] = True
        return ToolResult(
            success=success,
            output=_render(status, outcome),
            data=data,
            error=None if success else status,
        )


def _render(status: str, outcome: dict[str, Any]) -> str:
    branch = outcome.get("branch") or "?"
    if status == "merged":
        verified = outcome.get("verified")
        flag = "verified" if verified else "unverified"
        line = f"merged {branch} as {outcome.get('commit') or '?'} ({flag})"
        if outcome.get("cleanup_error"):
            line += f"\nwarning: candidate cleanup failed: {outcome['cleanup_error']}"
        return line
    if status in ("merge_skipped", "nothing_to_merge"):
        return f"nothing to merge from {branch}"
    if status == "conflict":
        paths = ", ".join(outcome.get("conflicting_paths") or ())
        return f"merge conflict in {branch}: {paths or '(unknown paths)'}"
    if status == "verify_failed":
        verify = outcome.get("verify") or {}
        tail = str(verify.get("output") or "").strip()
        return (
            f"verify failed for {branch} (exit {verify.get('exit_code')})"
            + (f"\n{tail}" if tail else "")
        )
    if status == "verify_unavailable":
        return f"verify is unavailable for {branch}; no merge was made"
    if status == "dirty":
        paths = ", ".join(outcome.get("conflicting_paths") or ())
        return "main tree is dirty; no merge was made" + (
            f": {paths}" if paths else ""
        )
    error = outcome.get("error") or status
    return f"merge {branch} failed: {error}"
