"""Codex-parity plan helpers (C-2/C-3).

Codex has no plan executor.  Its plan is a ``<proposed_plan>`` text block in the
assistant's final message, and execution is one ordinary turn whose prompt is
byte-for-byte ``"Implement the plan."``.  This module owns the two small pieces
that spelling needs: the block parser (shared by the display path and the
``plan_implement`` prompt card) and the thin runner.
"""

from __future__ import annotations

from typing import Any

from ai_coding.core.plan_runner import PlanReport

__all__ = [
    "CodexPlanRunner",
    "PROPOSED_PLAN_CLOSE",
    "PROPOSED_PLAN_OPEN",
    "extract_proposed_plan",
    "strip_proposed_plan",
]

PROPOSED_PLAN_OPEN = "<proposed_plan>"
PROPOSED_PLAN_CLOSE = "</proposed_plan>"

#: C-3: the literal Codex posts for an approved plan.  Not a template -- the
#: history carries everything else.
IMPLEMENT_PROMPT = "Implement the plan."


def _block_span(text: str) -> tuple[int, int, int, int] | None:
    """Offsets of the first block: (open_start, body_start, body_end, close_end).

    A missing closing tag still yields a block whose body runs to the end of
    the message.  Real providers (measured: qwen-plus) sometimes omit the close
    or leave it inside a code fence; treating that tail as "no plan" silently
    dropped the "Implement this plan?" gate.  The open tag is rare enough that
    the tail cannot be mistaken for prose, and a closed block still wins.
    """
    start = text.find(PROPOSED_PLAN_OPEN)
    if start < 0:
        return None
    body_start = start + len(PROPOSED_PLAN_OPEN)
    end = text.find(PROPOSED_PLAN_CLOSE, body_start)
    if end < 0:
        return start, body_start, len(text), len(text)
    return start, body_start, end, end + len(PROPOSED_PLAN_CLOSE)


def extract_proposed_plan(text: str) -> str | None:
    """The first ``<proposed_plan>`` body, stripped; ``None`` otherwise.

    Matches Codex's stream parser for a closed block; an unclosed one is taken
    to the end of the message (see ``_block_span``).  A trailing code fence --
    models often put the plan inside ```json -- is removed from the body.
    """
    span = _block_span(text or "")
    if span is None:
        return None
    _, body_start, body_end, _ = span
    body = text[body_start:body_end].strip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


def strip_proposed_plan(text: str) -> str:
    """The message without its first plan block, stripped (display only)."""
    span = _block_span(text or "")
    if span is None:
        return (text or "").strip()
    start, _, _, close_end = span
    return (text[:start] + text[close_end:]).strip()


class CodexPlanRunner:
    """The whole of Codex's execution: one ordinary turn, one literal prompt."""

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    async def run(
        self,
        session: Any,
        console: Any = None,
        *,
        on_delta: Any = None,
        observer: Any = None,
    ) -> PlanReport:
        settings = getattr(self._loop, "settings", None)
        session.plan_verify_items = bool(
            getattr(settings, "plan_verify_items", False)
        )
        session.plan_implementing = True
        try:
            await self._loop.process_input(
                session,
                IMPLEMENT_PROMPT,
                synthetic=True,
                on_delta=on_delta,
                observer=observer,
            )
        finally:
            session.plan_implementing = False
        task = getattr(session, "task", None)
        done, total = (task.done_count, task.total) if task is not None else (0, 0)
        if console is not None and total:
            console.info(f"[plan] {done}/{total} completed")
        return PlanReport()
