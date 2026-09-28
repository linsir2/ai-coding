"""Static HTML export of a recorded session (R9c).

One file, no external resources, no JavaScript: the trace is already on disk as
JSONL, and what it needs is a *view*.  Everything a trace can hold - tool
parameters, command output, error text - is attacker-adjacent (a tool can be told
to ``echo '<script>…'``), so every dynamic string goes through ``html.escape``:
the page must show code, never run it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from ai_coding.core.aggregate import AggregateReport
from ai_coding.core.trace import TraceStore
from ai_coding.ui.trace_render import summarize_turn

__all__ = [
    "SessionView",
    "SpanRow",
    "TurnView",
    "read_session_view",
    "render_index_html",
    "render_session_html",
]

_CSS = """
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 2rem; color: #222; }
h1 { font-size: 1.3rem; } .kpi { display: inline-block; margin-right: 1.5rem; }
.kpi b { display: block; font-size: 1.1rem; }
details { margin: .6rem 0; border: 1px solid #ddd; border-radius: 4px; padding: .4rem .6rem; }
summary { cursor: pointer; }
table { border-collapse: collapse; width: 100%; margin-top: .4rem; }
th, td {
  border-bottom: 1px solid #eee; text-align: left; padding: .15rem .4rem;
  vertical-align: top;
}
td.data { white-space: pre-wrap; word-break: break-word; max-width: 60ch; }
.muted { color: #777; }
"""


@dataclass(frozen=True)
class SpanRow:
    ts: str
    span: str
    parent: str
    iter: int
    evt: str
    data: dict[str, Any]


@dataclass(frozen=True)
class TurnView:
    turn: int
    path: Path
    spans: tuple[SpanRow, ...]
    summary: dict[str, Any]
    full: dict[str, Any] | None = None


@dataclass(frozen=True)
class SessionView:
    session: str
    turns: tuple[TurnView, ...]
    summary: dict[str, Any]
    full: bool = False


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _turn_number(path: Path) -> int:
    stem = path.stem.removeprefix("turn-")
    return int(stem) if stem.isdigit() else 0


def _read_spans(path: Path) -> tuple[SpanRow, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    spans: list[SpanRow] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        data = row.get("data")
        spans.append(
            SpanRow(
                ts=str(row.get("ts", "")),
                span=str(row.get("span", "")),
                parent=str(row.get("parent", "")),
                iter=_int(row.get("iter")),
                evt=str(row.get("evt", "")),
                data=dict(data) if isinstance(data, dict) else {},
            )
        )
    return tuple(spans)


def _read_full(path: Path) -> dict[str, Any] | None:
    candidate = path.with_suffix(".full.json")
    if not candidate.is_file():
        return None
    try:
        loaded = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_session_view(
    store: TraceStore, session: str, *, full: bool = False
) -> SessionView:
    """Read one session into a view object (no rendering, no policy)."""
    turns: list[TurnView] = []
    for path in sorted(store.session_dir(session).glob("turn-*.jsonl"), key=_turn_number):
        turns.append(
            TurnView(
                turn=_turn_number(path),
                path=path,
                spans=_read_spans(path),
                summary=summarize_turn(path),
                full=_read_full(path) if full else None,
            )
        )
    return SessionView(
        session=session,
        turns=tuple(turns),
        summary=store.read_summary(session),
        full=full,
    )


def render_session_html(view: SessionView) -> str:
    """One self-contained page for one session (R9c).  Pure."""
    summary = view.summary if isinstance(view.summary, dict) else {}
    kpis = "".join(
        f"<span class='kpi'>{escape(label)}<b>{escape(str(_int(summary.get(key, 0))))}</b></span>"
        for key, label in (
            ("turns", "turns"),
            ("steps", "steps"),
            ("prompt_tokens", "prompt"),
            ("completion_tokens", "completion"),
            ("cached_tokens", "cached"),
        )
    )
    note = " · <span class='muted'>includes message payloads</span>" if view.full else ""
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>trace {escape(view.session)}</title><style>{_CSS}</style></head><body>",
        f"<h1>session {escape(view.session)}{note}</h1>",
        f"<p>{kpis}</p>",
    ]
    for turn in view.turns:
        stats = turn.summary
        head = (
            f"turn {turn.turn} · {escape(str(_int(stats.get('spans', 0))))} spans · "
            f"{escape(str(_int(stats.get('model_steps', 0))))} steps · "
            f"{escape(str(_int(stats.get('tool_calls', 0))))} tools · "
            f"{escape(str(_int(stats.get('model_ms', 0)) + _int(stats.get('tool_ms', 0))))} ms"
        )
        parts.append(f"<details><summary>{head}</summary><table>")
        parts.append("<tr><th>ts</th><th>span</th><th>event</th><th>data</th></tr>")
        for row in turn.spans:
            body = escape(json.dumps(row.data, ensure_ascii=False))
            parts.append(
                "<tr>"
                f"<td>{escape(row.ts)}</td><td>{escape(row.span)}</td>"
                f"<td>{escape(row.evt)}</td><td class='data'>{body}</td>"
                "</tr>"
            )
        parts.append("</table>")
        if turn.full is not None:
            payload = escape(json.dumps(turn.full, ensure_ascii=False, indent=2))
            parts.append(
                "<details><summary>message payload</summary>"
                f"<pre>{payload}</pre></details>"
            )
        parts.append("</details>")
    parts.append("</body></html>")
    return "\n".join(parts)


def render_index_html(report: AggregateReport, views: list[SessionView]) -> str:
    """All sessions in one page: totals on top, each session's page inline (R9c)."""
    totals = report.totals
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<title>ai-coding traces</title>",
        f"<style>{_CSS}</style></head><body><h1>ai-coding traces</h1>",
        "<p>"
        f"<span class='kpi'>sessions<b>{len(report.rows)}</b></span>"
        f"<span class='kpi'>TOTAL turns<b>{totals.turns}</b></span>"
        f"<span class='kpi'>steps<b>{totals.steps}</b></span>"
        f"<span class='kpi'>prompt<b>{totals.prompt_tokens}</b></span>"
        f"<span class='kpi'>cached<b>{totals.cached_tokens}</b></span>"
        f"<span class='kpi'>hit%<b>{report.cache_ratio * 100:.1f}%</b></span>"
        "</p>",
    ]
    for view in views:
        parts.append(f"<h2>session {escape(view.session)}</h2>")
        parts.append(render_session_html(view))
    parts.append("</body></html>")
    return "\n".join(parts)
