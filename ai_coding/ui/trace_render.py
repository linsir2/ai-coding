"""Rendering traces for humans (R4a).

Pure functions over the JSONL a :class:`~ai_coding.core.trace.TraceRecorder`
wrote, so the CLI (and `/trace`) stay thin and the formatting is unit-testable
without touching a terminal (docs/r4-analysis.md 11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ai_coding.core.aggregate import AggregateReport

__all__ = ["render_budget", "render_overview", "render_turn", "summarize_turn"]


def _blank_summary() -> dict[str, Any]:
    return {
        "turn": 0,
        "spans": 0,
        "model_steps": 0,
        "tool_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "cached_reported": False,
        "model_ms": 0,
        "tool_ms": 0,
        "degraded": [],
        "guard_denied": 0,
        "tools": [],
    }


def _thousands(value: int) -> str:
    return f"{value:,}"


def _int(value: Any) -> int:
    """Best-effort int, treating anything unusable as zero."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def summarize_turn(path: str | Path) -> dict[str, Any]:
    """Fold one turn's JSONL into the numbers worth showing.  Never raises."""
    summary = _blank_summary()
    file_path = Path(path)
    if not file_path.is_file():
        return summary

    spans: set[str] = set()
    tools: list[str] = []
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return summary

    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        record: dict[str, Any] = parsed
        summary["turn"] = _int(record.get("turn")) or summary["turn"]
        span = record.get("span")
        if isinstance(span, str) and span:
            spans.add(span)
        event = record.get("evt")
        raw_data = record.get("data")
        data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
        if event == "ModelStepFinished":
            summary["model_steps"] += 1
            summary["model_ms"] += _int(data.get("latency_ms"))
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                summary["prompt_tokens"] += _int(usage.get("prompt_tokens"))
                summary["completion_tokens"] += _int(usage.get("completion_tokens"))
            cached = data.get("cached_tokens")
            if cached is not None:
                summary["cached_tokens"] += _int(cached)
                summary["cached_reported"] = True
        elif event == "ToolStarted":
            summary["tool_calls"] += 1
            name = data.get("tool_name")
            if isinstance(name, str):
                tools.append(name)
        elif event == "ToolFinished":
            summary["tool_ms"] += _int(data.get("duration_ms"))
            if data.get("error") in {"guard_denied", "guard_error"}:
                summary["guard_denied"] += 1
        elif event == "Degraded":
            component = data.get("component")
            if isinstance(component, str):
                summary["degraded"].append(component)

    summary["spans"] = len(spans)
    summary["tools"] = tools
    return summary


def render_turn(summary: Mapping[str, Any]) -> str:
    """One glance at a turn: what it cost, where the time went, what fell back."""
    turn = int(summary.get("turn", 0) or 0)
    prompt = int(summary.get("prompt_tokens", 0) or 0)
    completion = int(summary.get("completion_tokens", 0) or 0)
    total = prompt + completion
    parts = [
        f"turn {turn}: {_thousands(total)} tok "
        f"({_thousands(prompt)} in / {_thousands(completion)} out)"
    ]
    if summary.get("cached_reported") and prompt > 0:
        share = int(round(int(summary.get("cached_tokens", 0) or 0) / prompt * 100))
        parts.append(f"cache {share}%")
    parts.append(
        f"{int(summary.get('spans', 0) or 0)} spans · "
        f"{int(summary.get('model_steps', 0) or 0)} model steps · "
        f"{int(summary.get('tool_calls', 0) or 0)} tool calls"
    )
    model_ms = int(summary.get("model_ms", 0) or 0)
    tool_ms = int(summary.get("tool_ms", 0) or 0)
    if model_ms or tool_ms:
        parts.append(f"model {model_ms / 1000:.1f}s · tools {tool_ms / 1000:.1f}s")
    degraded = list(summary.get("degraded") or [])
    parts.append(f"{len(degraded)} degraded" + (f" ({', '.join(degraded)})" if degraded else ""))
    if summary.get("guard_denied"):
        parts.append(f"{int(summary['guard_denied'])} guard denial(s)")
    return " · ".join(parts)


def render_budget(
    summary: Mapping[str, Any], *, price: Any | None = None
) -> str:
    """Session-level totals from ``summary.json``.

    ``price`` is an optional ``core.budget.Pricing``.  When given and
    configured, an estimate is appended — split into fresh and cached input,
    because the two are billed at different rates.  Without it the line stays
    token-only: R4a's rule is that an unknown price yields no cost rather than a
    fake 0.0, and a session's summary is also what ``/budget`` reads.
    """
    prompt = int(summary.get("prompt_tokens", 0) or 0)
    completion = int(summary.get("completion_tokens", 0) or 0)
    if prompt == 0 and completion == 0:
        return "no recorded usage for this session"
    total = _thousands(prompt + completion)
    detail = f"({_thousands(prompt)} in / {_thousands(completion)} out)"
    parts = [
        f"{int(summary.get('turns', 0) or 0)} turns · "
        f"{int(summary.get('steps', 0) or 0)} steps · {total} tok {detail}"
    ]
    cached = int(summary.get("cached_tokens", 0) or 0)
    if cached and prompt > 0:
        parts.append(f"cache {int(round(cached / prompt * 100))}%")
    estimate = _estimate(prompt, completion, cached, price)
    if estimate is not None:
        parts.append(estimate)
    return " · ".join(parts)


def _estimate(
    prompt: int, completion: int, cached: int, price: Any | None
) -> str | None:
    """``≈ $x`` for a priced session, or ``None`` when the price is unknown."""
    if price is None or not getattr(price, "configured", False):
        return None
    cached = min(cached, prompt)
    fresh = max(prompt - cached, 0)
    cached_rate = getattr(price, "cached_per_1k", 0.0) or price.input_per_1k
    money = (
        fresh / 1000 * price.input_per_1k
        + cached / 1000 * cached_rate
        + completion / 1000 * price.output_per_1k
    )
    return f"≈ ¥{money:.4f}"


def render_overview(report: AggregateReport) -> str:
    """One table for every session plus a totals row (R9b).  Pure."""
    header = (
        f"{'SESSION':<20} {'TURNS':>5} {'FILES':>5} {'STEPS':>5} {'PROMPT':>9} "
        f"{'COMPL':>7} {'CACHED':>8} {'HIT%':>6} {'AGE':>6}"
    )
    lines = [header, "-" * len(header)]
    for row in report.rows:
        flag = "  !mismatch" if row.mismatch else ""
        lines.append(
            f"{row.session[:20]:<20} {row.turns:>5} {row.files:>5} {row.steps:>5} "
            f"{_thousands(row.prompt_tokens):>9} {_thousands(row.completion_tokens):>7} "
            f"{_thousands(row.cached_tokens):>8} {row.cache_ratio * 100:>5.1f}% "
            f"{row.age_days:>5.1f}d{flag}"
        )
    totals = report.totals
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<20} {totals.turns:>5} {totals.files:>5} {totals.steps:>5} "
        f"{_thousands(totals.prompt_tokens):>9} {_thousands(totals.completion_tokens):>7} "
        f"{_thousands(totals.cached_tokens):>8} {totals.cache_ratio * 100:>5.1f}%"
    )
    bad = [row.session for row in report.rows if row.mismatch]
    if bad:
        lines.append(f"! summary/disk mismatch in {len(bad)} session(s): {', '.join(bad)}")
    return "\n".join(lines)
