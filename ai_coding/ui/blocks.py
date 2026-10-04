"""Loop events → human-readable blocks (pure: no Textual, offline-testable).

The console-era renderer's R8c semantics — one throttled line per tool call,
quiet on a completed turn — live here, with an injectable clock so the throttle
can be tested without sleeping.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.text import Text

from ai_coding.core.events import (
    AssistantMessage,
    BackgroundTaskDone,
    IterationStarted,
    LedgerUpdated,
    LoopEvent,
    ScopedEvent,
    TaskUpdated,
    ToolChunk,
    ToolFinished,
    ToolStarted,
    ToolSurfaceWarning,
    TurnFinished,
)
from ai_coding.domain.loop import TurnStatus
from ai_coding.domain.message import ChatMessage

_PARAM_PREVIEW = 100
_OUTPUT_PREVIEW = 120
_CHUNK_THROTTLE_SECONDS = 0.15
_TARGET_COMMAND_LIMIT = 60
# WP3: backfill caps.  Folds upgrade preview → full at turn end; the caps keep
# one pathological call (10MB `cat`) from pinning the DOM, with a pointer to
# the session file where the untruncated text lives.
_FULL_OUTPUT_LIMIT = 20000
_FULL_PARAMS_LIMIT = 2000
# R1-4: a step's thinking is rendered whole-ish but bounded — qwen-class
# reasoning runs to thousands of chars, and an unbounded fold pins the DOM
# exactly like an unbounded tool output would (same cap discipline as WP3).
_REASONING_LIMIT = 4000
# R1-4: nested sub-agent lines are one-liners by contract (a timeline, not a
# log); longer child text stays in the trace and the ledger file.
_NESTED_LINE_LIMIT = 120


@dataclass(frozen=True)
class Block:
    """One rendered unit of transcript content."""

    kind: str
    text: str
    style: str = ""
    call_id: str = ""
    tool_name: str = ""
    tool_target: str = ""
    tool_status: str = ""  # "" | "running" | "ok" | "failed"
    # Tool-card display (fold title + settled card): the past-tense verb
    # ("运行命令") and the kind label ("Shell").  Empty for non-tool blocks.
    verb: str = ""
    kind_label: str = ""

    def render(self) -> Text:
        return Text(self.text, style=self.style)


# Fold-card display table: tool name -> (kind label, verb).  Kind labels stay
# English (the reference card shows "Shell"), verbs follow the reference
# Chinese.  Unknown tools (incl. MCP `server__tool` names) run bare under
# their own name so they stay distinguishable.
_TOOL_CARD_META: dict[str, tuple[str, str]] = {
    "bash": ("Shell", "运行命令"),
    "read": ("Read", "读取文件"),
    "edit": ("Edit", "编辑文件"),
    "write": ("Write", "写入文件"),
    "glob": ("Glob", "查找文件"),
    "todo_write": ("Todo", "更新计划"),
    "subAgent": ("Subagent", "委派子任务"),
    "skill": ("Skill", "读取技能"),
    "memory_search": ("Memory", "搜索记忆"),
    "memory_write": ("Memory", "记录记忆"),
    "checkpoint_write": ("Checkpoint", "记录检查点"),
    "ask_user": ("Ask", "询问用户"),
}


def tool_card_meta(tool_name: str) -> tuple[str, str]:
    """(kind label, verb) for a tool call, with an unknown-tool fallback."""
    return _TOOL_CARD_META.get(tool_name, (tool_name, "运行工具"))


def tool_card_identity(tool_name: str, params: dict[str, Any]) -> tuple[str, str, str]:
    """(verb, target, kind) for a fold title — the same table the live view uses.

    History replay builds its folds through this so a reopened card reads
    exactly like the card that settled at the time.
    """
    kind, verb = tool_card_meta(tool_name)
    return verb, _tool_target(tool_name, params), kind


def params_preview(params: dict[str, Any]) -> str:
    """The one-line params body a fold shows while its full text resolves."""
    return _short(json.dumps(params, ensure_ascii=False), _PARAM_PREVIEW)


def tool_title(*, mark: str, verb: str, target: str, running: bool = False) -> str:
    """Fold title: ``▸ 正在运行命令 · ls`` while running, ``✓ 已运行命令 · ls`` done."""
    action = f"正在{verb}" if running else f"已{verb}"
    return f"{mark} {action} · {target}" if target else f"{mark} {action}"


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _tool_target(tool_name: str, params: dict[str, Any]) -> str:
    """Human-readable target for a tool call: a path or a truncated command."""
    if tool_name == "glob":
        # glob_tool.py:45 names it ``pattern``; there is no ``path`` key.
        return str(params.get("pattern", ""))
    if tool_name in {"read", "write", "edit"}:
        return str(params.get("path", ""))
    if tool_name == "bash":
        return _short(str(params.get("command", "")), _TARGET_COMMAND_LIMIT)
    return ""


@dataclass(frozen=True)
class FullCallView:
    """One tool call resolved back to its full text from session messages.

    Events deliberately carry only previews (R1a-3), so the transcript upgrades
    each fold preview → full at turn end by looking the call back up here.
    Either side may be ``None`` (unknown call, empty output); the caller then
    keeps whatever preview the fold already shows.
    """

    params: str | None
    output: str | None


def _strip_envelope(content: str) -> str:
    """Drop the ``<tool_output>`` wrapper and its machine ``<data>`` block."""
    kept: list[str] = []
    in_data = False
    for index, line in enumerate(content.splitlines()):
        stripped = line.strip()
        if index == 0 and line.startswith("<tool_output"):
            continue
        if stripped == "<data>":
            in_data = True
            continue
        if stripped == "</data>":
            in_data = False
            continue
        if in_data or stripped == "</tool_output>":
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def tool_outcome_from_message(message: ChatMessage) -> tuple[bool, str]:
    """(success, display text) for a persisted ``role=tool`` message.

    The envelope marks failure with ``ok="false"`` (``domain/run.py``); the
    machine ``<data>`` block is dropped the same way the live view does, so a
    replayed card shows exactly what the settled card showed at the time.
    """
    content = message.content or ""
    success = 'ok="false"' not in content.splitlines()[0] if content else True
    return success, _strip_envelope(content)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n…（全文 {len(text)} 字符，只显示前 {limit} 字符，完整内容见会话文件）"
    )


def _params_line(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return arguments
    if isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
        return f"$ {parsed['command']}"
    return arguments


def full_call_view(messages: Sequence[ChatMessage], call_id: str) -> FullCallView:
    """Find a call's full params line and output in session messages.

    Later messages win on both sides (a retried call id resolves to its last
    outcome).  Caps apply per side; ``None`` means "nothing to upgrade with".
    """
    params: str | None = None
    output: str | None = None
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            for ref in msg.tool_calls:
                if ref.id == call_id:
                    params = _params_line(ref.arguments)
        if msg.role == "tool" and msg.tool_call_id == call_id and msg.content:
            output = _strip_envelope(msg.content) or None
    if params is not None:
        params = _cap(params, _FULL_PARAMS_LIMIT)
    if output is not None:
        output = _cap(output, _FULL_OUTPUT_LIMIT)
    return FullCallView(params=params, output=output)


def blocks_from_event(event: LoopEvent) -> Block | None:
    """Map one loop event to a block, or ``None`` when it is not shown."""
    if isinstance(event, IterationStarted):
        # WP1: the event still marks turn boundaries for the trace recorder
        # (core/trace.py), but per-step "[turn x/y]" markers are noise in the
        # transcript — and they mounted after the content they mark.
        return None
    if isinstance(event, ToolStarted):
        params = _short(json.dumps(event.params, ensure_ascii=False), _PARAM_PREVIEW)
        kind_label, verb = tool_card_meta(event.tool_name)
        return Block(
            "tool",
            params,
            call_id=event.call_id,
            tool_name=event.tool_name,
            tool_target=_tool_target(event.tool_name, event.params),
            tool_status="running",
            verb=verb,
            kind_label=kind_label,
        )
    if isinstance(event, ToolFinished):
        preview = _short(event.output_preview or event.error or "", _OUTPUT_PREVIEW)
        return Block(
            "tool",
            preview,
            call_id=event.call_id,
            tool_name=event.tool_name,
            tool_status="ok" if event.success else "failed",
        )
    if isinstance(event, ToolChunk):
        return Block(
            "tool-chunk", f"│ {_short(event.text, _OUTPUT_PREVIEW)}", "dim", call_id=event.call_id
        )
    if isinstance(event, ToolSurfaceWarning):
        # R7e-3c parity: the old console showed the number, the threshold and
        # the top origins so the operator knows which server to trim.
        origins = " · ".join(f"{name}={chars}" for name, chars in event.origins[:3])
        suffix = f"  {origins}" if origins else ""
        return Block(
            "notice",
            f"[tools] {event.tools_chars} 字符（阈值 {event.threshold}）{suffix}",
            "dim",
        )
    if isinstance(event, TaskUpdated):
        return Block("task", event.task.render(), "dim")
    if isinstance(event, LedgerUpdated):
        # R0-2: the turn's open unknowns, rendered dim — a question, not a
        # result.  Emitted only when the set moves, so this block marks the
        # moment something became unknown (or stopped being so).
        lines = [f"[{item.level}] {item.question}" for item in event.items]
        for item in event.items:
            lines.extend(f"  · {bit}" for bit in item.evidence)
        return Block("ledger", "\n".join(lines), "dim")
    if isinstance(event, TurnFinished) and event.outcome.status is TurnStatus.TIMEOUT:
        return Block("turn-end", f"[turn] timed out: {event.outcome.reason}", "red")
    if isinstance(event, TurnFinished) and event.outcome.truncated_by_length:
        # W1: a length cutoff reads as "done" everywhere else (COMPLETED, and
        # even MAX when the last step was cut) — it explains the visible
        # symptom, so it takes precedence over the budget notices.
        return Block(
            "turn-end",
            "[turn] 回答因长度上限被截断：输入「继续」可接上",
            "red",
        )
    if isinstance(event, TurnFinished) and event.outcome.status is TurnStatus.MAX_ITERATIONS:
        # WP2: budget cuts were silent (only TIMEOUT rendered), so a capped
        # turn read as "the model stopped mid-sentence".  Say it was the
        # budget and name the way back; COMPLETED stays silent by contract.
        return Block(
            "turn-end",
            f"[turn] 步数用完（{event.outcome.iterations} 步），任务未收尾："
            "输入「继续」可接上",
            "red",
        )
    if isinstance(event, AssistantMessage):
        # R1-4: prose travels the delta stream — mapping the event too would
        # render every answer twice.  Only the thinking rides this event, and
        # only when the endpoint produced any.
        if not event.reasoning:
            return None
        return Block("reasoning", _cap(event.reasoning, _REASONING_LIMIT), "dim")
    if isinstance(event, ScopedEvent):
        return _nested_block(event)
    if isinstance(event, BackgroundTaskDone):
        # R1-2: a background child settled — possibly turns after the spawning
        # turn ended.  One dim line with the verdict; the full packet lives in
        # the ledger file (same pointer discipline as WP3).
        mark = "✓" if event.status == "completed" else "✗"
        line = f"{mark} 子任务完成 [{event.name}]（{event.status}）"
        if event.origin_session:
            line += f" · 会话 {event.origin_session[:12]}"
        if event.summary:
            line += f"：{_short(event.summary, _NESTED_LINE_LIMIT)}"
        if event.ledger_path:
            line += f" · 完整回包: {event.ledger_path}"
        return Block("background", line, "dim")
    return None


def _nested_block(event: ScopedEvent) -> Block | None:
    """One dim timeline line for delegated work (R1-4/A2).

    The child stays a black box at the reasoning level (R1a-5), but its
    *behaviour* — started/finished calls and spoken conclusions — is what
    the parent's transcript was missing.  Everything fine-grained (chunks,
    deltas, step telemetry, assembly) stays in the trace: the transcript is
    a timeline, not a log.
    """
    inner = event.event
    tag = f"↳ [{event.scope}]"
    if isinstance(inner, ToolStarted):
        kind, _ = tool_card_meta(inner.tool_name)
        target = _tool_target(inner.tool_name, inner.params)
        line = f"{tag} ▶ {kind} {target}".rstrip()
        return Block("subagent", line, "dim")
    if isinstance(inner, ToolFinished):
        mark = "✓" if inner.success else "✗"
        detail = _short(inner.output_preview or inner.error or "", _NESTED_LINE_LIMIT)
        line = f"{tag} {mark} {inner.tool_name} {detail}".rstrip()
        return Block("subagent", line, "dim")
    if isinstance(inner, AssistantMessage):
        # The child's spoken conclusion — capped to one line.  Its thinking
        # stays inside the child (R1a-5); only what it said travels up.
        if not inner.text:
            return None
        return Block("subagent", f"{tag} {_short(inner.text, _NESTED_LINE_LIMIT)}", "dim")
    return None


class TranscriptObserver:
    """LoopObserver → blocks.  The chunk throttle lives here (R8c)."""

    def __init__(
        self,
        append: Callable[[Block], None],
        *,
        clock: Callable[[], float] | None = None,
        chunks: bool = True,
    ) -> None:
        self._append = append
        self._clock = clock or time.monotonic
        self._last_chunk: dict[str, float] = {}
        self._chunks = bool(chunks)

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, ToolChunk):
            if not self._chunks:
                return
            now = self._clock()
            previous = self._last_chunk.get(event.call_id)
            if previous is not None and now - previous < _CHUNK_THROTTLE_SECONDS:
                return
            self._last_chunk[event.call_id] = now
        block = blocks_from_event(event)
        if block is not None:
            self._append(block)
