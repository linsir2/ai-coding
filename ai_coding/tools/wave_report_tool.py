"""``wave_report`` -- the model's structured handoff at the end of a plan wave.

A wave is one continuous session; the next one starts from a handoff.  The
executor already knows what finished, what was skipped, which files changed and
which proofs passed -- this tool carries only the things it cannot know:
interfaces introduced, decisions the next wave must keep, pitfalls, and one
hint.  The executor merges the two at the wave boundary; the mechanical half
always wins (see ``domain/plan_wave.py``).

Only visible inside a running wave (``BaseTool.wave_only``); a call made from
memory is refused by ``execute`` as well.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.domain.plan_wave import (
    WAVE_HANDOFF_HINT_MAX_CHARS,
    WAVE_HANDOFF_ITEM_MAX_CHARS,
    WAVE_HANDOFF_MAX_ITEMS,
)
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

_LIST_FIELDS = ("interfaces", "decisions", "pitfalls")


class WaveReportTool(BaseTool):
    """Hand over what this wave learned to the next wave."""

    parallel_safe = True
    mutates_world = False
    wave_only = True

    @property
    def name(self) -> str:
        return "wave_report"

    @property
    def description(self) -> str:
        return (
            "Record what the NEXT wave must know when it starts fresh: interfaces "
            "you introduced or changed, decisions it must keep, pitfalls that do "
            "not work, and one hint. Files changed and proofs are recorded by the "
            "executor -- do not repeat them. Call this once, near the end of your "
            "wave; set needs_replan=true only when the remaining items are wrong "
            "and should be rewritten."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "interfaces": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": WAVE_HANDOFF_ITEM_MAX_CHARS},
                    "maxItems": WAVE_HANDOFF_MAX_ITEMS,
                    "description": "New/changed interface signatures the next wave must call.",
                },
                "decisions": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": WAVE_HANDOFF_ITEM_MAX_CHARS},
                    "maxItems": WAVE_HANDOFF_MAX_ITEMS,
                    "description": "Decisions made this wave that later items must follow.",
                },
                "pitfalls": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": WAVE_HANDOFF_ITEM_MAX_CHARS},
                    "maxItems": WAVE_HANDOFF_MAX_ITEMS,
                    "description": "Dead ends, failures, or traps the next wave must avoid.",
                },
                "next_hint": {
                    "type": "string",
                    "maxLength": WAVE_HANDOFF_HINT_MAX_CHARS,
                    "description": "One sentence for the next wave (optional).",
                },
                "needs_replan": {
                    "type": "boolean",
                    "description": (
                        "True when the remaining items themselves are wrong and "
                        "should be rewritten before more work runs."
                    ),
                },
            },
        }

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        resolved = resolve_ctx(ctx)
        task = resolved.task
        if task.armed_profile != "plan" or not task.wave_active:
            return ToolResult(
                success=False,
                output="error: wave_report is only available inside a running plan wave.",
                error="not_in_wave",
            )
        report, error = _validated(params)
        if error is not None:
            return ToolResult(success=False, output=f"error: {error}", error="invalid_report")
        task.wave_report = report
        counts = "、".join(f"{name} {len(report[name])}" for name in _LIST_FIELDS)
        flag = "needs_replan" if report["needs_replan"] else "no replan"
        return ToolResult(
            success=True,
            output=f"handoff recorded ({counts or 'no lines'}; {flag}).",
            data={"recorded": True, "needs_replan": report["needs_replan"]},
        )


def _validated(params: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Normalise the report; refuse over-cap input instead of truncating it."""
    report: dict[str, Any] = {}
    for name in _LIST_FIELDS:
        raw = params.get(name, [])
        if raw is None:
            raw = []
        # Models write one line as a string often enough that refusing it cost
        # two calls in the measured run.  A single line is the same report.
        if isinstance(raw, str):
            raw = [raw] if raw.strip() else []
        if not isinstance(raw, list):
            return {}, f"'{name}' must be a list of strings"
        if len(raw) > WAVE_HANDOFF_MAX_ITEMS:
            return {}, (
                f"'{name}' has {len(raw)} lines; at most {WAVE_HANDOFF_MAX_ITEMS} "
                "are allowed (merge the rest)"
            )
        lines: list[str] = []
        for entry in raw:
            if not isinstance(entry, str):
                return {}, f"'{name}' entries must be strings"
            text = entry.strip()
            if not text:
                continue
            if len(text) > WAVE_HANDOFF_ITEM_MAX_CHARS:
                return {}, (
                    f"'{name}' line is {len(text)} chars; at most "
                    f"{WAVE_HANDOFF_ITEM_MAX_CHARS} are allowed"
                )
            lines.append(text)
        report[name] = lines
    hint = params.get("next_hint", "")
    if hint is None:
        hint = ""
    if not isinstance(hint, str):
        return {}, "'next_hint' must be a string"
    hint = hint.strip()
    if len(hint) > WAVE_HANDOFF_HINT_MAX_CHARS:
        return {}, (
            f"'next_hint' is {len(hint)} chars; at most "
            f"{WAVE_HANDOFF_HINT_MAX_CHARS} are allowed"
        )
    report["next_hint"] = hint
    report["needs_replan"] = bool(params.get("needs_replan", False))
    return report, None


__all__ = ["WaveReportTool"]
