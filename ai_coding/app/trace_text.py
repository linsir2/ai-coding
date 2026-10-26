"""Rendering traces for humans (R4a) — pure text output.

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
        # The per-layer account of the last CompactionEnded in this turn (M-E3):
        # /trace renders what the pipeline actually did, not just the message
        # counts.  The layer list is self-contained — the boundary estimates
        # are its first ``before_tokens`` and last ``after_tokens`` — so the
        # summary carries no second copy of them.
        "compaction_layers": [],
        # The sub-agent slice of the totals above.  Kept as a split rather than
        # subtracted, so the totals still mean "what this turn cost".
        "subagent_scopes": 0,
        "subagent_model_steps": 0,
        "subagent_prompt_tokens": 0,
    }


def _thousands(value: int) -> str:
    return f"{value:,}"


def _layer_line(layer: Mapping[str, Any]) -> str:
    """One layer's share of the compaction line, by token delta.

    Counts alone would mislead: L2/L3 rewrite a body and keep the message, so
    their real effect is the token drop, which is what this shows (plan-m-e
    §C.1 / §C.2).  L4 is labelled as a summary rather than a count.
    """
    name = str(layer.get("name", ""))
    removed = _int(layer.get("removed"))
    rewritten = _int(layer.get("rewritten"))
    skipped = _int(layer.get("skipped"))
    before = _int(layer.get("before_tokens"))
    after = _int(layer.get("after_tokens"))
    tail = f"{before}→{after} token（估）"
    if name == "L4":
        return f"L4 摘要 {tail}"
    if removed:
        return f"{name} −{removed} 条 · {tail}"
    segs: list[str] = []
    if rewritten:
        segs.append(f"改写 {rewritten} 条")
    if skipped:
        segs.append(f"跳过 {skipped} 条")
    segs.append(tail)
    return f"{name} " + " · ".join(segs)


def _compaction_line(layers: list[Mapping[str, Any]]) -> str:
    """The whole compaction part: one segment per layer, plus L4's absence.

    A layer that did not run is *absent* from ``layers``, never all-zero
    (plan-m-e §C.1 P2-1), so the line names it explicitly rather than letting a
    reader assume L4 ran and produced an empty summary.
    """
    segments = [_layer_line(layer) for layer in layers]
    if not any(str(layer.get("name", "")) == "L4" for layer in layers):
        segments.append("L4 未触发")
    return "压缩：" + " / ".join(segments)


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
    scopes: set[str] = set()
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
        # A sub-agent's events arrive with `scope` beside `evt`, never as an
        # event name of their own, so this key is the only marker there is.
        scope = record.get("scope")
        scoped = False
        if isinstance(scope, str) and scope:
            scopes.add(scope)
            scoped = True
        event = record.get("evt")
        raw_data = record.get("data")
        data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
        if event == "ModelStepFinished":
            summary["model_steps"] += 1
            summary["model_ms"] += _int(data.get("latency_ms"))
            usage = data.get("usage")
            prompt = 0
            if isinstance(usage, Mapping):
                prompt = _int(usage.get("prompt_tokens"))
                summary["prompt_tokens"] += prompt
                summary["completion_tokens"] += _int(usage.get("completion_tokens"))
            if scoped:
                summary["subagent_model_steps"] += 1
                summary["subagent_prompt_tokens"] += prompt
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
        elif event == "CompactionEnded":
            # Fold the lifecycle (started/delta/ended) into one line's worth of
            # facts: the per-layer account.  Deltas are lossy and the started
            # event carries nothing to summarise.  Keys are the trace's own
            # snake_case (``dataclasses.asdict``), not the camelCase the wire
            # bridge emits.
            summary["compaction_layers"] = list(data.get("layers") or [])

    summary["spans"] = len(spans)
    summary["subagent_scopes"] = len(scopes)
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
    # Only when there were any: a turn that delegated nothing must not grow a
    # zero-valued tail, and most turns do not delegate.
    if int(summary.get("subagent_scopes", 0) or 0):
        parts.append(
            f"subagents {int(summary['subagent_scopes'])} · "
            f"{int(summary.get('subagent_model_steps', 0) or 0)}/"
            f"{int(summary.get('model_steps', 0) or 0)} steps · "
            f"{_thousands(int(summary.get('subagent_prompt_tokens', 0) or 0))}/"
            f"{_thousands(prompt)} prompt"
        )
    model_ms = int(summary.get("model_ms", 0) or 0)
    tool_ms = int(summary.get("tool_ms", 0) or 0)
    if model_ms or tool_ms:
        parts.append(f"model {model_ms / 1000:.1f}s · tools {tool_ms / 1000:.1f}s")
    degraded = list(summary.get("degraded") or [])
    parts.append(f"{len(degraded)} degraded" + (f" ({', '.join(degraded)})" if degraded else ""))
    # Only when a compaction happened: a turn that never rewrote its history
    # must not grow a zero-valued tail, exactly like the sub-agent line.
    layers = list(summary.get("compaction_layers") or [])
    if layers:
        parts.append(_compaction_line(layers))
    if summary.get("guard_denied"):
        parts.append(f"{int(summary['guard_denied'])} guard denial(s)")
    return " · ".join(parts)


def render_budget(
    summary: Mapping[str, Any],
    *,
    price: Any | None = None,
    prices: Mapping[str, Any] | None = None,
) -> str:
    """Session-level totals from ``summary.json``.

    ``prices`` maps a model name to its ``core.budget.Pricing`` (G7).  With a
    fallback chain one session is billed by two providers, and the totals alone
    cannot say which one the money went to.  ``price`` is the older single-model
    form and stays supported: a caller with one model -- or a summary written
    before per-model attribution existed -- has nothing to split.

    When given and configured, an estimate is appended, split into fresh and
    cached input because the two are billed at different rates.  Without a price
    the line stays token-only: R4a's rule is that an unknown price yields no cost
    rather than a fake 0.0.

    A per-model breakdown appears only once more than one model was used: for a
    single model it would repeat the total on a second line.
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

    by_model = summary.get("by_model")
    rows = _model_rows(by_model, prices) if isinstance(by_model, dict) else None
    if rows is None:
        estimate = _estimate(prompt, completion, cached, price)
        if estimate is not None:
            parts.append(estimate)
        return " · ".join(parts)

    # The total is still the headline; the split explains it.  Summed from the
    # models that have a price, so an unpriced one neither adds nor hides money.
    money_total = 0.0
    any_priced = False
    for row in rows:
        if row[3] is not None:
            money_total += row[3]
            any_priced = True
    if any_priced:
        parts.append(f"≈ ¥{money_total:.4f}")
    body = " · ".join(parts)
    width = max(len(str(row[0])) for row in rows)
    lines = [body]
    for name, prompt_t, completion_t, money in rows:
        line = (
            f"  {str(name):<{width}}  "
            f"{_thousands(prompt_t)} in / {_thousands(completion_t)} out"
        )
        if money is not None:
            line += f"  ≈ ¥{money:.4f}"
        lines.append(line)
    return "\n".join(lines)


def _model_rows(
    by_model: Mapping[str, Any], prices: Mapping[str, Any] | None
) -> list[tuple[str, int, int, float | None]] | None:
    """One row per model, or ``None`` when a breakdown would say nothing.

    Sorted by name so the output is stable between runs; a session with a single
    attributed model returns ``None`` and the caller falls back to one line.
    """
    if len(by_model) < 2:
        return None
    rows: list[tuple[str, int, int, float | None]] = []
    for name in sorted(by_model):
        spend = by_model[name]
        if not isinstance(spend, Mapping):
            continue
        prompt_t = int(spend.get("prompt_tokens", 0) or 0)
        completion_t = int(spend.get("completion_tokens", 0) or 0)
        cached_t = int(spend.get("cached_tokens", 0) or 0)
        price = (prices or {}).get(name)
        money = None
        if price is not None and getattr(price, "configured", False):
            cached_t = min(cached_t, prompt_t)
            fresh = max(prompt_t - cached_t, 0)
            cached_rate = getattr(price, "cached_per_1k", 0.0) or price.input_per_1k
            money = (
                fresh / 1000 * price.input_per_1k
                + cached_t / 1000 * cached_rate
                + completion_t / 1000 * price.output_per_1k
            )
        rows.append((name, prompt_t, completion_t, money))
    return rows or None


def _money_number(
    prompt: int, completion: int, cached: int, price: Any | None
) -> float | None:
    """Money for one model's spend, or ``None`` when the price is unknown."""
    if price is None or not getattr(price, "configured", False):
        return None
    cached = min(cached, prompt)
    fresh = max(prompt - cached, 0)
    cached_rate = getattr(price, "cached_per_1k", 0.0) or price.input_per_1k
    return float(
        fresh / 1000 * price.input_per_1k
        + cached / 1000 * cached_rate
        + completion / 1000 * price.output_per_1k
    )


def _estimate(
    prompt: int, completion: int, cached: int, price: Any | None
) -> str | None:
    """``≈ ¥x`` for a priced session, or ``None`` when the price is unknown."""
    money = _money_number(prompt, completion, cached, price)
    if money is None:
        return None
    return f"≈ ¥{money:.4f}"


def budget_figures(
    summary: Mapping[str, Any],
    *,
    price: Any | None = None,
    prices: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The numbers behind ``render_budget``, for consumers that are not prose.

    The sidebar's session card is the second reader of the same summary (M4b);
    without this it would grow its own copy of the cost formula.  ``cost`` is
    ``None`` when no price is known (R4a: no fake 0.0), and ``cachedPercent``
    is ``None`` when the provider never reported cached tokens.
    """
    prompt = int(summary.get("prompt_tokens", 0) or 0)
    completion = int(summary.get("completion_tokens", 0) or 0)
    cached = int(summary.get("cached_tokens", 0) or 0)

    cost: float | None = None
    by_model = summary.get("by_model")
    if isinstance(by_model, dict) and prices:
        total = 0.0
        priced = False
        for model_name, spend in by_model.items():
            if not isinstance(spend, dict):
                continue
            money = _money_number(
                int(spend.get("prompt_tokens", 0) or 0),
                int(spend.get("completion_tokens", 0) or 0),
                int(spend.get("cached_tokens", 0) or 0),
                prices.get(str(model_name)),
            )
            if money is not None:
                total += money
                priced = True
        if priced:
            cost = total
    if cost is None:
        cost = _money_number(prompt, completion, cached, price)

    return {
        "tokens": prompt + completion,
        "cost": cost,
        "cachedPercent": (
            int(round(cached / prompt * 100)) if cached and prompt > 0 else None
        ),
    }


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
