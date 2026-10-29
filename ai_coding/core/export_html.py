"""Static HTML export of a recorded session (R9c, redesigned in R20b).

One file, no external resources, no JavaScript: the trace is already on disk as
JSONL, and what it needs is a *view*.  Everything a trace can hold -- tool
parameters, command output, error text -- is attacker-adjacent (a tool can be told
to ``echo '<script>…'``), so every dynamic string goes through ``html.escape``:
the page must show code, never run it.

R20b replaced the flat span table with a structured report: KPI cards, one card
per turn with a status badge and a token bar, a paired tool timeline, the model's
prose with lightweight syntax highlighting, the compaction layers, and the
degradations.  Colours come from the TUI's own token table so the page reads as
the product rather than as a debug dump.

The theme follows ``prefers-color-scheme`` and can be forced with a checkbox --
**no JavaScript**, because the no-``<script>`` rule is what keeps tool output
from executing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from ai_coding.app.trace_text import TurnFacts, fold_turn
from ai_coding.core.aggregate import AggregateReport
from ai_coding.core.export_highlight import escape as _esc
from ai_coding.core.export_highlight import guess_language, highlight
from ai_coding.core.trace import TraceStore

__all__ = [
    "SessionView",
    "SpanRow",
    "TurnView",
    "read_session_view",
    "render_index_html",
    "render_session_html",
]


# --------------------------------------------------------------------- palette
#
# Values mirror ``ai_coding/frontend/src/theme/tokens.ts`` so the export and the
# TUI are one product.  ``--fg``/``--bg`` are the two knobs the theme flips.

_DARK = {
    "--bg": "#0a0a0a",
    "--card": "#1e1e1e",
    "--fg": "#eeeeee",
    "--muted": "#808080",
    "--accent": "#9d7cd8",
    "--ok": "#7fd88f",
    "--info": "#56b6c2",
    "--warn": "#f5a742",
    "--err": "#e06c75",
    "--border": "#484848",
    "--kw": "#c678dd",
    "--str": "#98c379",
    "--com": "#5c6370",
    "--num": "#d19a66",
    "--key": "#e06c75",
    "--add": "#4fd6be",
    "--del": "#c53b53",
}
_LIGHT = {
    "--bg": "#ffffff",
    "--card": "#ececec",
    "--fg": "#1a1a1a",
    "--muted": "#707070",
    "--accent": "#7a5fb8",
    "--ok": "#3d9e52",
    "--info": "#2a8f9e",
    "--warn": "#b8791f",
    "--err": "#c53b53",
    "--border": "#b8b8b8",
    "--kw": "#7a5fb8",
    "--str": "#3d9e52",
    "--com": "#6b7280",
    "--num": "#b8791f",
    "--key": "#c53b53",
    "--add": "#1f9d85",
    "--del": "#c53b53",
}


def _css() -> str:
    dark = "\n".join(f"  {k}: {v};" for k, v in _DARK.items())
    light = "\n".join(f"  {k}: {v};" for k, v in _LIGHT.items())
    return f"""
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
/* Default follows the OS. The two checkboxes force a side -- no JavaScript, and
   ``:has()`` is what lets the inputs live inside their labels (so they can show
   a focus ring) while still overriding the page's variables.  The override must
   land on ``body``, not ``.page``: ``body``'s own ``color`` is resolved from the
   variables at ``body`` level, and a light override one level down left every
   inherited colour light-on-white. */
@media (prefers-color-scheme: dark) {{ body.theme-system {{ {dark}
}} }}
@media (prefers-color-scheme: light) {{ body.theme-system {{ {light}
}} }}
body:has(#force-light:checked):not(:has(#force-dark:checked)) {{ {light}
}}
body:has(#force-dark:checked):not(:has(#force-light:checked)) {{ {dark}
}}
.page {{ max-width: 70rem; margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }}
.page-head {{
  display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); padding-bottom: .75rem; margin-bottom: 1rem;
}}
.brand {{ font-size: 1.25rem; font-weight: 700; letter-spacing: -.02em; }}
.brand .muted {{ font-weight: 400; font-size: .95rem; }}
.meta {{ color: var(--muted); font-size: .82rem; }}
.theme-switch {{ margin-left: auto; display: flex; gap: .5rem; align-items: center; }}
.theme-opt {{
  display: inline-flex; align-items: center; font-size: .8rem; color: var(--muted);
  border: 1px solid var(--border); border-radius: 4px; padding: .15rem .5rem;
  cursor: pointer;
}}
.theme-opt input {{ position: absolute; opacity: 0; width: 1px; height: 1px; }}
.theme-opt input:checked + span {{ color: var(--fg); }}
/* The switch must be usable from the keyboard: the input is visually hidden, so
   the ring has to be drawn on the label it belongs to. */
.theme-opt input:focus-visible + span {{ outline: 2px solid var(--accent); }}
.theme-opt span {{ display: inline-block; }}
h2 {{ font-size: 1rem; color: var(--accent); margin: 1.5rem 0 .5rem; font-weight: 700; }}
.kpis {{ display: flex; flex-wrap: wrap; gap: .6rem; margin-bottom: 1.25rem; }}
.kpi {{
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  padding: .5rem .8rem; min-width: 6.5rem;
}}
.kpi-value {{ display: block; font-size: 1.25rem; font-weight: 700; }}
.kpi-accent .kpi-value {{ color: var(--accent); }}
.kpi-info .kpi-value {{ color: var(--info); }}
.kpi-ok .kpi-value {{ color: var(--ok); }}
.kpi-label {{ color: var(--muted); font-size: .75rem; }}
details.turn {{
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  border-left: 3px solid var(--border);
  margin-bottom: .6rem; padding: .5rem .8rem;
}}
/* The status is legible from the card's edge as well as its badge. */
details.turn.turn-ok {{ border-left-color: var(--ok); }}
details.turn.turn-warn {{ border-left-color: var(--warn); }}
details.turn.turn-err {{ border-left-color: var(--err); }}
details.turn > summary {{
  cursor: pointer; display: flex; gap: .6rem; align-items: center; flex-wrap: wrap;
  list-style: none;
}}
details.turn > summary::-webkit-details-marker {{ display: none; }}
.turn-no {{ font-weight: 700; }}
.turn-sub {{ color: var(--muted); font-size: .8rem; }}
.badge {{
  font-size: .72rem; padding: .1rem .45rem; border-radius: 999px;
  border: 1px solid var(--border); color: var(--muted);
}}
.badge-ok {{ color: var(--ok); border-color: var(--ok); }}
.badge-err {{ color: var(--err); border-color: var(--err); }}
.badge-warn {{ color: var(--warn); border-color: var(--warn); }}
.badge-info {{ color: var(--info); border-color: var(--info); }}
.turn-body {{ padding: .6rem 0 .2rem; }}
.metrics {{ display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: .6rem;
            color: var(--muted); font-size: .82rem; }}
.bar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden;
       margin: .3rem 0 .6rem; background: var(--border); }}
.bar i {{ display: block; height: 100%; }}
.bar-prompt {{ background: var(--accent); }}
.bar-completion {{ background: var(--info); }}
.bar-cached {{ background: var(--ok); }}
.timeline {{ margin: .4rem 0; }}
.tool-row {{
  display: grid; grid-template-columns: 1.4rem 6.5rem 1fr auto auto;
  gap: .5rem; align-items: baseline; padding: .18rem 0;
  border-bottom: 1px dotted var(--border); font-size: .85rem;
}}
.tool-row:last-child {{ border-bottom: 0; }}
.tool-icon {{ color: var(--accent); }}
.tool-name {{ font-weight: 700; }}
.tool-params {{ color: var(--muted); overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }}
.tool-ms {{ color: var(--muted); font-size: .78rem; }}
.tool-out {{
  grid-column: 3 / -1; margin: .15rem 0 .3rem; padding: .35rem .5rem;
  background: var(--bg); border-left: 2px solid var(--border);
  white-space: pre-wrap; word-break: break-word; font-size: .8rem;
}}
.prose {{ margin: .5rem 0; padding: .5rem .6rem; background: var(--bg);
          border-radius: 4px; white-space: pre-wrap; word-break: break-word;
          font-size: .85rem; }}
.prose pre {{ margin: .4rem 0; padding: .4rem .5rem; background: var(--card);
             border-radius: 4px; overflow-x: auto; white-space: pre; }}
.layers {{ display: flex; flex-wrap: wrap; gap: .4rem; margin: .4rem 0; }}
.layer {{ font-size: .76rem; padding: .15rem .5rem; border-radius: 4px;
         border: 1px solid var(--border); color: var(--muted); }}
.degraded {{
  margin: .4rem 0; padding: .35rem .5rem; font-size: .8rem;
  border-left: 3px solid var(--warn); background: var(--card); color: var(--warn);
}}
.issues li {{ margin: .2rem 0; }}
.muted {{ color: var(--muted); }}
footer.page-foot {{
  margin-top: 2rem; padding-top: .75rem; border-top: 1px solid var(--border);
  color: var(--muted); font-size: .78rem;
}}
/* token colours, driven by export_highlight */
.tok-com {{ color: var(--com); }}
.tok-str {{ color: var(--str); }}
.tok-num {{ color: var(--num); }}
.tok-kw {{ color: var(--kw); }}
.tok-key {{ color: var(--key); }}
.tok-lit {{ color: var(--num); }}
.tok-add {{ color: var(--add); }}
.tok-del {{ color: var(--del); }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
"""


# ------------------------------------------------------------------ data model

@dataclass(frozen=True)
class SpanRow:
    ts: str
    span: str
    parent: str
    iter: int
    evt: str
    data: dict[str, Any]
    scope: str = ""


@dataclass(frozen=True)
class TurnView:
    turn: int
    path: Path
    spans: tuple[SpanRow, ...]
    summary: dict[str, Any]
    full: dict[str, Any] | None = None
    facts: TurnFacts | None = None


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
                scope=str(row.get("scope", "")),
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
        spans = _read_spans(path)
        turns.append(
            TurnView(
                turn=_turn_number(path),
                path=path,
                spans=spans,
                summary=_summarize(path),
                full=_read_full(path) if full else None,
                facts=fold_turn(spans),
            )
        )
    return SessionView(
        session=session,
        turns=tuple(turns),
        summary=store.read_summary(session),
        full=full,
    )


def _summarize(path: Path) -> dict[str, Any]:
    """The R9c summary, kept so the CLI's ``/trace`` text view still works."""
    from ai_coding.app.trace_text import summarize_turn

    return summarize_turn(path)


# ------------------------------------------------------------------ rendering

_TOOL_ICONS = {
    "read": "⏎",
    "write": "✎",
    "edit": "✏",
    "bash": "▶",
    "grep": "🔍",
    "glob": "📁",
    "webfetch": "🌐",
    "websearch": "🔎",
    "memory_search": "🧠",
    "memory_write": "📌",
    "checkpoint_write": "🏁",
    "present_plan": "📋",
    "todo_write": "☑",
    "subAgent": "🤖",
    "ask_user": "❓",
    "job_output": "📄",
    "job_list": "📋",
    "job_kill": "⏹",
    "skill": "🧩",
    "mcp_call": "🔌",
    "finish_research": "🏁",
    "history_search": "🕘",
}


def _badge(text: str, kind: str = "", *, full: str = "") -> str:
    """A pill.  ``full`` keeps the whole text in ``title`` when the pill is
    clipped -- a diagnostic is often longer than a badge should be."""
    title = f' title="{escape(full)}"' if full and full != text else ""
    return f'<span class="badge {kind}"{title}>{escape(text)}</span>'


#: One status, two readings: the badge's colour and the card's left edge.
_STATUS = {
    "completed": ("badge-ok", "turn-ok"),
    "aborted": ("badge-warn", "turn-warn"),
    "max_iterations": ("badge-warn", "turn-warn"),
    "budget": ("badge-warn", "turn-warn"),
    "timeout": ("badge-err", "turn-err"),
}


def _status_badge(status: str) -> str:
    kind, _edge = _STATUS.get(status, ("", ""))
    return _badge(status or "unknown", kind)


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _tool_language(name: str, params: str) -> str:
    """Which highlighter fits a tool's output."""
    if name == "bash":
        return "bash"
    if name in {"grep", "glob", "job_output", "job_list", "history_search"}:
        return "text"
    if name in {"read", "write", "edit"}:
        for suffix in (".py", ".ts", ".tsx", ".js", ".json", ".md", ".yaml", ".toml"):
            if suffix in params:
                return guess_language(f"x{suffix}")
    return "text"


def _render_prose(text: str) -> str:
    """The model's prose, with fenced code blocks highlighted.

    Fences are matched first so a code sample is one highlighted block rather than
    a run of markdown tokens.
    """
    if not text:
        return ""
    parts = []
    buffer: list[str] = []
    language = ""
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if buffer:
                parts.append(_render_prose_plain("".join(buffer)))
                buffer = []
            if language:
                language = ""
            else:
                language = stripped[3:].strip() or "text"
            continue
        if language:
            parts.append(highlight(line, language))
        else:
            buffer.append(line)
    if buffer:
        parts.append(_render_prose_plain("".join(buffer)))
    return "".join(parts)


def _render_prose_plain(text: str) -> str:
    """Prose outside a fence: escape, keep line breaks."""
    return f"<div>{escape(text)}</div>"


def _render_turn(turn: TurnView) -> str:
    facts = turn.facts or fold_turn(turn.spans)
    total = facts.prompt_tokens + facts.completion_tokens
    cached = facts.cached_tokens
    cached_share = (cached / facts.prompt_tokens * 100) if facts.prompt_tokens else 0
    head = (
        f'<span class="turn-no">turn {turn.turn}</span>'
        f"{_status_badge(facts.status)}"
        f'<span class="turn-sub">'
        f"{facts.iterations}"
        f"{'/' + str(facts.max_iterations) if facts.max_iterations else ''} 次迭代"
        f" · {total:,} tok"
        f"{f' · cache {cached_share:.0f}%' if cached else ''}"
        f"</span>"
    )
    body: list[str] = []

    if total:
        prompt_pct = facts.prompt_tokens / total * 100
        completion_pct = facts.completion_tokens / total * 100
        cached_pct = cached / total * 100
        bar = (
            '<div class="bar" role="img"'
            f' aria-label="prompt {facts.prompt_tokens:,}, '
            f'completion {facts.completion_tokens:,}, cached {cached:,}">'
            f'<i class="bar-prompt" style="width:{prompt_pct:.1f}%"></i>'
            f'<i class="bar-completion" style="width:{completion_pct:.1f}%"></i>'
            f'<i class="bar-cached" style="width:{cached_pct:.1f}%"></i>'
            "</div>"
        )
        body.append(
            '<div class="metrics">'
            f"<span>prompt {facts.prompt_tokens:,}</span>"
            f"<span>completion {facts.completion_tokens:,}</span>"
            f"<span>cached {cached:,}</span>"
            f"<span>model {_seconds(facts.model_ms)}</span>"
            f"<span>tools {_seconds(facts.tool_ms)}</span>"
            f"<span>{len(facts.tools)} 次工具调用</span>"
            "</div>" + bar
        )

    if facts.tools:
        rows = []
        for call in facts.tools:
            icon = _TOOL_ICONS.get(call.name, "•")
            if call.success:
                outcome = _badge("ok", "badge-ok")
            else:
                label = call.error or "failed"
                clipped = label if len(label) <= 28 else label[:27] + "…"
                outcome = _badge(clipped, "badge-err", full=label)
            rows.append(
                '<div class="tool-row">'
                f'<span class="tool-icon">{icon}</span>'
                f'<span class="tool-name">{escape(call.name)}</span>'
                f'<span class="tool-params">{escape(call.params)}</span>'
                f'<span class="tool-ms">{_seconds(call.duration_ms)}</span>'
                f"{outcome}"
                "</div>"
            )
            if call.output:
                rows.append(
                    f'<div class="tool-out">'
                    f"{highlight(call.output, _tool_language(call.name, call.params))}"
                    "</div>"
                )
        body.append('<div class="timeline">' + "".join(rows) + "</div>")

    prose = facts.answer or facts.prose
    if prose:
        body.append(f'<div class="prose">{_render_prose(prose)}</div>')

    if facts.layers:
        chips = []
        for layer in facts.layers:
            name = str(layer.get("name", ""))
            removed = _int(layer.get("removed"))
            before = _int(layer.get("before_tokens"))
            after = _int(layer.get("after_tokens"))
            label = name if name else "layer"
            if removed:
                label += f" −{removed} 条"
            if before or after:
                label += f" · {before}→{after} tok"
            chips.append(f'<span class="layer">{escape(label)}</span>')
        if chips:
            body.append(
                '<div class="metrics"><span>压缩</span></div><div class="layers">'
                + "".join(chips)
                + "</div>"
            )

    for component, reason, fallback in facts.degraded:
        body.append(
            '<div class="degraded">'
            f"<b>{escape(component)}</b>: {escape(reason)}"
            f"{' → ' + escape(fallback) if fallback else ''}"
            "</div>"
        )

    if facts.guard_denied:
        body.append(
            f'<div class="degraded">{facts.guard_denied} 次守卫拒绝（guard_denied）</div>'
        )

    if turn.full is not None:
        payload = _esc(json.dumps(turn.full, ensure_ascii=False, indent=2))
        body.append(
            "<details><summary>message payload</summary>"
            f"<pre>{payload}</pre></details>"
        )

    _, edge = _STATUS.get(facts.status, ("", ""))
    return (
        f'<details class="turn {edge}"{"" if turn.turn > 1 else " open"}>'
        f"<summary>{head}</summary>"
        f'<div class="turn-body">{"".join(body)}</div>'
        "</details>"
    )


def _theme_switch() -> str:
    """The two force switches.  A checkbox inside its own label, so the hidden
    input can still draw a focus ring on the visible text -- no JavaScript."""
    return (
        '<span class="theme-switch">'
        '<label class="theme-opt" for="force-light">'
        '<input type="checkbox" id="force-light"><span>亮色</span></label>'
        '<label class="theme-opt" for="force-dark">'
        '<input type="checkbox" id="force-dark"><span>暗色</span></label>'
        "</span>"
    )


def _kpis(items: Sequence[tuple[str, str, str]]) -> str:
    """One row of KPI cards: ``(value, label, kind)``.

    The wrapper is the flex container: the cards are flex items only as its
    children, and a page that emitted them straight into ``<main>`` stacked six
    full-width blocks down the page (R20b).  One builder for the three pages so
    the wrapper cannot go missing from one of them again.  ``kind`` tints the
    number (``accent``/``info``/``ok``) or is empty for the neutral default.
    """
    cards = []
    for value, label, kind in items:
        extra = f" kpi-{kind}" if kind else ""
        cards.append(
            f'<div class="kpi{extra}"><span class="kpi-value">{escape(value)}</span>'
            f'<span class="kpi-label">{escape(label)}</span></div>'
        )
    return '<div class="kpis">' + "".join(cards) + "</div>"


def render_session_html(view: SessionView) -> str:
    """One self-contained page for one session.  Pure."""
    summary = view.summary if isinstance(view.summary, dict) else {}
    prompt = _int(summary.get("prompt_tokens"))
    completion = _int(summary.get("completion_tokens"))
    cached = _int(summary.get("cached_tokens"))
    turns = _int(summary.get("turns"))
    steps = _int(summary.get("steps"))
    tools = sum(len(t.facts.tools) if t.facts else 0 for t in view.turns)
    cache_share = (cached / prompt * 100) if prompt else 0

    kpis = _kpis([
        (str(turns), "回合", ""),
        (str(steps), "步", ""),
        (f"{prompt:,}", "prompt tok", "accent"),
        (f"{completion:,}", "completion tok", "info"),
        (f"{cache_share:.0f}%", "缓存命中", "ok"),
        (str(tools), "工具调用", ""),
    ])
    note = (
        " · <span class='muted'>includes message payloads</span>" if view.full else ""
    )
    body = "".join(_render_turn(turn) for turn in view.turns)
    issues = _render_issues(view)
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>ai-coding 会话报告 · {escape(view.session)}</title>"
        f"<style>{_css()}</style></head>"
        "<body class='theme-system'>"
        '<div class="page">'
        '<header class="page-head">'
        '<span class="brand">ai-coding <span class="muted">会话报告</span></span>'
        f'<span class="meta">session {escape(view.session)}{note}</span>'
        f"{_theme_switch()}"
        "</header>"
        f"<main>{kpis}{issues}{body}</main>"
        '<footer class="page-foot">'
        "generated by <code>ai-coding /export html</code> · "
        "self-contained, no external resources"
        "</footer>"
        "</div></body></html>"
    )


def _render_issues(view: SessionView) -> str:
    items: list[str] = []
    for turn in view.turns:
        facts = turn.facts or fold_turn(turn.spans)
        if facts.guard_denied:
            items.append(
                f"turn {turn.turn}: {facts.guard_denied} 次守卫拒绝"
            )
        for component, reason, _fallback in facts.degraded:
            items.append(f"turn {turn.turn}: {component} — {reason}")
    if not items:
        return ""
    return (
        "<h2>已知问题</h2><ul class='issues'>"
        + "".join(f"<li>{escape(item)}</li>" for item in items)
        + "</ul>"
    )


def render_index_html(report: AggregateReport, views: list[SessionView]) -> str:
    """All sessions in one page: totals on top, each session inline.

    R20b fixed the nested-document bug: the per-session pages are now rendered as
    fragments (no ``<!doctype>/<html>/<head>/<style>``) so the index is one valid
    document rather than a document inside a body.
    """
    totals = report.totals
    kpis = _kpis([
        (str(len(report.rows)), "会话", ""),
        (str(totals.turns), "回合", ""),
        (str(totals.steps), "步", ""),
        (f"{totals.prompt_tokens:,}", "prompt tok", "accent"),
        (f"{totals.completion_tokens:,}", "completion tok", "info"),
        (f"{totals.cache_ratio * 100:.1f}%", "缓存命中", "ok"),
    ])
    sections = []
    for view in views:
        sections.append(
            f"<h2>session {escape(view.session)}</h2>"
            + _render_session_fragment(view)
        )
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>ai-coding traces</title>"
        f"<style>{_css()}</style></head>"
        "<body class='theme-system'>"
        "<div class='page'>"
        '<header class="page-head">'
        '<span class="brand">ai-coding <span class="muted">traces</span></span>'
        f"{_theme_switch()}"
        "</header>"
        f"<main><h2>总计（TOTAL）</h2>{kpis}{''.join(sections)}</main>"
        '<footer class="page-foot">generated by '
        "<code>ai-coding /export html</code></footer>"
        "</div></body></html>"
    )


def _render_session_fragment(view: SessionView) -> str:
    """One session's report without the document wrapper (for the index page)."""
    summary = view.summary if isinstance(view.summary, dict) else {}
    turns = _int(summary.get("turns"))
    steps = _int(summary.get("steps"))
    prompt = _int(summary.get("prompt_tokens"))
    completion = _int(summary.get("completion_tokens"))
    cached = _int(summary.get("cached_tokens"))
    kpis = _kpis([
        (str(turns), "回合", ""),
        (str(steps), "步", ""),
        (f"{prompt:,}", "prompt tok", "accent"),
        (f"{completion:,}", "completion tok", "info"),
        (f"{cached:,}", "cached tok", "ok"),
    ])
    body = "".join(_render_turn(turn) for turn in view.turns)
    return f"<section>{kpis}{_render_issues(view)}{body}</section>"
