"""What one turn leaves behind, and what to say when it did not finish (R6a).

Compaction is lossy in the middle (measured, docs/r6-analysis.md 1.1): at 60
turns the message list is untouched, yet all but the last three tool bodies become
``[Previous: used bash]``.  The head goal and the last twenty messages survive;
"how did I get here" does not.  One live run then spent three extra iterations and
grew prompt tokens from 3,363 to 6,363 digging through the 270 KB session file
(docs/r6-analysis.md 1.2).

``TurnDigest`` is the *harness-derived* answer: no extra model call, just the four
things the loop already knows when a turn ends — the user's request, the terminal
outcome, the artifacts it produced, and how many todo items are still open.  The
ring is rendered into a volatile tail block only when the history was actually
rewritten (``core.compaction.Compactor.did_compact``); in an ordinary turn the
recent conversation is still in the history, so the ring would be pure
duplication (docs/r6-analysis.md 2.1, D-R6-2).

``render_handoff`` is the other half: a turn that exhausts its budget with
``outcome.text is None`` used to hand the user an empty string (docs/r6-analysis.md
1.3).  The handoff is *harness-authored*, so it lives in ``TurnOutcome.handoff``
and never in ``text``: ``core.memory_turn`` reads ``text is None`` to decide
whether the turn concluded anything, and ``core.subagent`` reads ``text`` as the
model's own conclusion (D-R6-4).

Values live in ``domain`` — like ``TaskState``, which also owns its ``render()`` —
so nothing here depends on ``core``.  ``append_digest`` deliberately takes the
session as ``Any`` rather than importing ``SessionData``, which would be a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolArtifact
from ai_coding.domain.task import TaskState

__all__ = [
    "DEFAULT_NOTE_CHARS",
    "DEFAULT_OUTCOME_CHARS",
    "DEFAULT_REQUEST_CHARS",
    "WORKING_MEMORY_NOTE",
    "TurnDigest",
    "append_digest",
    "build_digest",
    "one_line",
    "render_handoff",
    "render_working_memory",
]

DEFAULT_REQUEST_CHARS = 60
DEFAULT_OUTCOME_CHARS = 90
#: R8a: how long the model's own line may be.  The ring has 1096 characters of
#: per-line budget after its fixed overhead (measured, docs/r8-analysis.md 1.1).
DEFAULT_NOTE_CHARS = 120
_ELLIPSIS = "…"
_MAX_FILES_SHOWN = 4
_WHITESPACE = re.compile(r"\s+")

WORKING_MEMORY_NOTE = "需要更早的细节时用 memory_search 查证，不要整读会话文件。"

# The policy says "no progress" when it stops a turn that keeps trying and keeps
# failing; tests/test_turn_policy_escalation.py pins that phrase.  The report must
# not turn it into "the budget ran out", because it did not.
_STALL_PREFIX = "no progress"

_STATUS_LABEL: dict[TurnStatus, str] = {
    TurnStatus.MAX_ITERATIONS: "迭代预算用尽",
    TurnStatus.BUDGET: "token 预算用尽",
    TurnStatus.TIMEOUT: "回合超时",
    TurnStatus.ERROR: "引擎错误",
    TurnStatus.STOPPED: "用户中止",
    TurnStatus.ABORTED: "回合被取消",
}


def one_line(text: str, limit: int) -> str:
    """Collapse whitespace and cut to ``limit`` characters — never multi-line."""
    body = _WHITESPACE.sub(" ", str(text or "")).strip()
    if limit <= 0 or len(body) <= limit:
        return body
    return body[: max(1, limit - 1)].rstrip() + _ELLIPSIS


def _unique_paths(artifacts: Sequence[ToolArtifact]) -> tuple[str, ...]:
    """Artifact paths, de-duplicated, in the order they appeared."""
    seen: list[str] = []
    for artifact in artifacts:
        path = str(getattr(artifact, "path", "") or "")
        if path and path not in seen:
            seen.append(path)
    return tuple(seen)


@dataclass(frozen=True)
class TurnDigest:
    """One turn, in one line."""

    turn: int
    request: str
    outcome: str
    status: str = ""
    files: tuple[str, ...] = ()
    open_items: int = 0
    #: R8a: what the model asked its future self to remember.  Empty means this
    #: line is entirely harness-derived (the R6a behaviour).
    note: str = ""
    #: "model" when ``note`` came from ``checkpoint_write``, else "harness".
    source: str = "harness"

    def display(self) -> str:
        """The sentence the ring shows: the model's note wins when there is one."""
        return self.note or self.outcome

    def render(self) -> str:
        label = f"t{self.turn}" if self.turn else "t?"
        line = f"- {label} {self.request or '(无请求)'}"
        shown = self.display()
        if shown:
            line += f" → {shown}"
        extras: list[str] = []
        if self.files:
            extras.append("改了 " + ", ".join(self.files[:_MAX_FILES_SHOWN]))
        if self.status and self.status != TurnStatus.COMPLETED.value:
            extras.append(self.status)
        if extras:
            line += f"（{'；'.join(extras)}）"
        return line

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "turn": int(self.turn),
            "request": self.request,
            "outcome": self.outcome,
            "status": self.status,
        }
        if self.files:
            data["files"] = list(self.files)
        if self.open_items:
            data["openItems"] = int(self.open_items)
        if self.note:
            data["note"] = self.note
        # Written even when it is "harness", so the round trip is exact and a
        # reloaded session keeps answering "who wrote this line?".
        data["source"] = self.source or "harness"
        return data

    @staticmethod
    def from_dict(data: Any) -> TurnDigest | None:
        """Parse one persisted row; ``None`` for anything unusable (untrusted input)."""
        if not isinstance(data, dict):
            return None
        try:
            turn = int(data.get("turn", 0) or 0)
        except (TypeError, ValueError):
            return None
        request = str(data.get("request", "") or "")
        if turn <= 0 and not request:
            return None
        raw_files = data.get("files")
        files = (
            tuple(str(item) for item in raw_files)
            if isinstance(raw_files, (list, tuple))
            else ()
        )
        try:
            open_items = int(data.get("openItems", 0) or 0)
        except (TypeError, ValueError):
            open_items = 0
        return TurnDigest(
            turn=turn,
            request=request,
            outcome=str(data.get("outcome", "") or ""),
            status=str(data.get("status", "") or ""),
            files=files,
            open_items=open_items,
            note=str(data.get("note", "") or ""),
            source=str(data.get("source", "") or "harness"),
        )


def build_digest(
    *,
    turn: int,
    user_input: str,
    outcome: TurnOutcome,
    artifacts: Sequence[ToolArtifact] = (),
    task: TaskState | None = None,
    request_chars: int = DEFAULT_REQUEST_CHARS,
    outcome_chars: int = DEFAULT_OUTCOME_CHARS,
    model_note: str = "",
    note_chars: int = DEFAULT_NOTE_CHARS,
) -> TurnDigest:
    """The one line this turn leaves in the session (harness-derived, no model call)."""
    spoken = one_line(outcome.text or "", outcome_chars)
    if not spoken:
        label = _STATUS_LABEL.get(outcome.status, outcome.status.value)
        reason = one_line(outcome.reason, outcome_chars)
        spoken = f"{label}：{reason}" if reason else label
    note = one_line(model_note, note_chars) if model_note else ""
    return TurnDigest(
        turn=int(turn),
        request=one_line(user_input, request_chars),
        outcome=spoken,
        status=outcome.status.value,
        files=_unique_paths(artifacts),
        open_items=len(task.open_items()) if task is not None else 0,
        note=note,
        source="model" if note else "harness",
    )


def append_digest(session: Any, digest: TurnDigest, *, keep: int) -> None:
    """Append to the session's bounded ring (oldest first).  Never raises."""
    try:
        ring = list(getattr(session, "turn_digests", None) or [])
        ring.append(digest)
        limit = int(keep)
        session.turn_digests = ring[-limit:] if limit > 0 else ring
    except Exception:  # noqa: BLE001 — a digest must never break a turn
        return


def render_working_memory(
    digests: Sequence[TurnDigest],
    *,
    current_turn: int,
    max_chars: int = 1200,
    compacted: bool = True,
    note: str = WORKING_MEMORY_NOTE,
) -> str:
    """The volatile ``<working-memory>`` block, or ``""`` when it adds nothing.

    ``compacted`` is the loop's answer to "was this request's history rewritten?"
    (``Compactor.did_compact``).  Newest lines win; the newest one is kept even if
    it alone exceeds the budget, because dropping it would hide the most recent
    turn exactly when it matters.
    """
    if not compacted or max_chars <= 0:
        return ""
    lines = [digest.render() for digest in digests if (digest.request or digest.outcome)]
    if not lines:
        return ""
    header = f"当前: 第 {current_turn} 轮（更早的消息已被压缩，以下是每轮一行摘要）"
    overhead = len("<working-memory>\n\n</working-memory>") + len(header) + 1
    if note:
        overhead += len(note) + 1
    budget = max_chars - overhead
    chosen: list[str] = []
    used = 0
    for line in reversed(lines):
        if chosen and used + len(line) + 1 > budget:
            break
        chosen.append(line)
        used += len(line) + 1
    if not chosen:
        return ""
    chosen.reverse()
    body = [header, *chosen]
    if note:
        body.append(note)
    return "<working-memory>\n" + "\n".join(body) + "\n</working-memory>"


def render_handoff(
    *,
    outcome: TurnOutcome,
    task: TaskState | None = None,
    failures: Sequence[tuple[str, str, str]] = (),
    artifacts: Sequence[ToolArtifact] = (),
    trace_path: str = "",
    max_items: int = 5,
    continuations: int = 0,
) -> str:
    """What the harness says when the model ran out of turn without a final word.

    Returns ``""`` when the model did speak: the handoff must never overwrite the
    model's own conclusion (docs/r6-analysis.md 2.1).  A turn that *completed*
    normally is not "unfinished" either — a blank answer there is the model's own
    (unhelpful) ending, and inventing a report for it would misreport the status.
    """
    if (outcome.text or "").strip() or outcome.status is TurnStatus.COMPLETED:
        return ""
    label = _STATUS_LABEL.get(outcome.status, outcome.status.value)
    if outcome.reason.startswith(_STALL_PREFIX):
        label = "零进展（提前收尾）"
    lines = [f"[回合未完成 · {label}（{outcome.iterations} 轮）]"]
    if outcome.reason:
        lines.append(f"原因: {one_line(outcome.reason, 160)}")
    if task is not None and task.goal:
        lines.append(f"目标: {one_line(task.goal, 160)}")
    if task is not None and task.items:
        done = [item for item in task.items if item.is_terminal()]
        if done:
            lines.append(
                "已完成: "
                + "；".join(
                    f"{item.id}. {one_line(item.content, 60)}" for item in done[:max_items]
                )
            )
        open_items = task.open_items()
        if open_items:
            lines.append(
                "未完成: "
                + "；".join(
                    f"{item.id}. {one_line(item.content, 60)}" for item in open_items[:max_items]
                )
            )
    if failures:
        tool, reason, preview = failures[-1]
        tail = f"（{one_line(preview, 80)}）" if preview else ""
        lines.append(f"最近一次失败: {tool} → {reason}{tail}")
    paths = _unique_paths(artifacts)
    if paths:
        lines.append("产物: " + ", ".join(paths[:_MAX_FILES_SHOWN]))
    if trace_path:
        lines.append(f"轨迹: {trace_path}")
    if continuations:
        lines.append(f"（已自动续跑 {continuations} 次）")
    return "\n".join(lines)
