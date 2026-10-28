"""Clipping text for the model without silently dropping one end.

Every cap in this repo used to keep one end: ``text[:limit]`` for tool output
and ``text[-limit:]`` for the eval driver's feedback.  Both are wrong for the
thing they clip — a test run puts its failures in the middle and its summary at
the end, so head-only loses the verdict while tail-only loses the first
traceback.  Measured in eval-08's full-stack task: the model was handed 1,276
characters that ended with 500 characters of a pytest deprecation warning and
one of eighteen failures.

Head **and** tail, with the omission stated in the middle, is what OpenCode
does for its compaction transcript (``clip``, compaction.ts:1043-1050) and what
Claude Code does for oversized tool output.
"""

from __future__ import annotations

__all__ = ["clip_head_tail"]


def clip_head_tail(
    text: str,
    limit: int,
    *,
    head_ratio: float = 0.6,
    note: str = "",
) -> str:
    """``text`` at most about ``limit`` characters, keeping both ends.

    ``head_ratio`` decides the split: 0.6 keeps slightly more of the beginning,
    because that is where a tool's identifying output lives (the command echo,
    the first traceback, the file's opening lines) while the end holds the
    verdict (exit summary, ``FAILED`` list, last lines of a log).

    The marker always states how many characters were omitted, and ``note``
    appends where the whole text can be read (a persisted-output path, a job
    log).  A clip the model cannot notice is a clip it will reason from as if
    nothing were missing.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    ratio = min(0.9, max(0.1, float(head_ratio)))
    head = max(1, int(limit * ratio))
    tail = max(1, limit - head)
    omitted = len(text) - head - tail
    if omitted <= 0:  # ``limit`` was 1-2 characters; keep it whole instead
        return text
    where = f" · {note}" if note else ""
    # ``truncated`` stays in the marker: it is the word existing readers (and
    # the model, which has seen it in every tool result so far) recognise.
    marker = f"\n... [truncated: 省略 {omitted} 字符{where}] ...\n"
    return f"{text[:head]}{marker}{text[-tail:]}"
