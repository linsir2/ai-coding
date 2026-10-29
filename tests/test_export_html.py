"""R20b: the redesigned session report — structure, theme, highlighting, a11y.

The R9c tests pin that the page is escaped, offline and stable.  These pin what
the redesign added: KPI cards, a per-turn token bar, a paired tool timeline,
syntax-highlighted prose, the TUI's token palette, and a valid index document.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_coding.core.aggregate import SessionRow, aggregate_sessions
from ai_coding.core.export_html import read_session_view, render_index_html, render_session_html
from ai_coding.core.trace import TraceStore


def _row(**over) -> dict:
    base = {
        "v": 1, "ts": "2026-10-28T00:00:00.000Z", "session": "sess-1", "turn": 1,
        "span": "i1", "parent": "turn", "iter": 1, "evt": "IterationStarted",
        "data": {},
    }
    base.update(over)
    return base


def _session(tmp_path: Path, *, rows: list[dict] | None = None) -> TraceStore:
    store = TraceStore(tmp_path / "traces", workspace=tmp_path)
    writer = store.begin_turn("sess-1")
    for row in rows if rows is not None else [_row()]:
        writer.write_line(json.dumps(row))
    (store.session_dir("sess-1") / "summary.json").write_text(
        json.dumps({"turns": 1, "steps": 1, "prompt_tokens": 100,
                    "completion_tokens": 20, "cached_tokens": 40}),
        encoding="utf-8",
    )
    return store


# ------------------------------------------------------------------ structure


def test_the_report_has_kpi_cards(tmp_path):
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert "kpi" in html
    assert "回合" in html and "缓存命中" in html


def test_the_kpi_cards_sit_in_one_flex_row(tmp_path):
    """The cards are flex items only if they share a ``.kpis`` parent; without it
    they were six full-width blocks stacked down the page."""
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert html.count('<div class="kpis">') == 1
    start = html.index('<div class="kpis">')
    end = html.index("</div></div>", start)
    assert html.count('class="kpi', start, end) >= 5


def test_the_index_totals_sit_in_one_flex_row(tmp_path):
    store = _session(tmp_path)
    view = read_session_view(store, "sess-1")
    report = aggregate_sessions((SessionRow("sess-1", turns=1, files=1, prompt_tokens=10),))
    html = render_index_html(report, [view])
    assert html.count('<div class="kpis">') == 2   # totals + the session section
    assert "kpi-accent" in html and "kpi-ok" in html


def test_a_completed_turn_gets_a_green_left_edge(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="TurnFinished", span="turn",
             data={"outcome": {"status": "TurnStatus.COMPLETED", "text": "done"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "turn-ok" in html


def test_each_turn_gets_a_status_badge(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="TurnFinished", span="turn",
             data={"outcome": {"status": "TurnStatus.COMPLETED", "reason": "model finished",
                               "iterations": 3, "text": "done"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "completed" in html
    assert "badge-ok" in html


def test_a_failed_turn_gets_the_warning_badge(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="TurnFinished", span="turn",
             data={"outcome": {"status": "TurnStatus.ABORTED", "reason": "user escaped",
                               "iterations": 2, "text": ""}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "aborted" in html
    assert "badge-warn" in html


def test_the_token_bar_shows_all_three_components(tmp_path):
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert "bar-prompt" in html and "bar-completion" in html and "bar-cached" in html


def test_the_tool_timeline_pairs_started_and_finished(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="ToolStarted", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "read", "params": {"path": "a.py"}}),
        _row(evt="ToolFinished", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "read", "success": True,
                   "output_preview": "     1  hello\n", "duration_ms": 12}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "tool-row" in html
    assert "read" in html
    assert "path=a.py" in html
    assert "12ms" in html


def test_a_tool_that_never_finished_still_appears(tmp_path):
    """An interrupted turn still asked for the call; hiding it would erase that."""
    store = _session(tmp_path, rows=[
        _row(evt="ToolStarted", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "bash", "params": {"command": "sleep 99"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "bash" in html
    assert "unfinished" in html


def test_a_denied_tool_shows_the_error_badge(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="ToolStarted", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "write", "params": {"path": "x.py"}}),
        _row(evt="ToolFinished", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "write", "success": False,
                   "error": "guard_denied", "duration_ms": 1}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "guard_denied" in html
    assert "badge-err" in html


def test_a_long_error_is_clipped_but_kept_in_the_tooltip(tmp_path):
    """A diagnostic belongs in the pill only up to a point; the rest rides the
    ``title`` so the table stays readable."""
    long_error = "user-denied-by-user: " + "x" * 80
    store = _session(tmp_path, rows=[
        _row(evt="ToolStarted", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "bash", "params": {"command": "ls"}}),
        _row(evt="ToolFinished", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "bash", "success": False,
                   "error": long_error, "duration_ms": 1}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "…</span>" in html
    assert f'title="{long_error}"' in html


def test_the_compaction_layers_render_as_chips(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="CompactionEnded", span="i1",
             data={"layers": [{"name": "L1", "removed": 5, "before_tokens": 1000,
                               "after_tokens": 900}]}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "layer" in html
    assert "L1" in html and "−5 条" in html


def test_degradations_render_as_warning_bars(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="Degraded", span="i1",
             data={"component": "model_step", "reason": "provider rejected",
                   "fallback": "compacted and retried"}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "degraded" in html
    assert "model_step" in html and "compacted and retried" in html


def test_the_model_prose_is_rendered(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="TurnFinished", span="turn",
             data={"outcome": {"status": "TurnStatus.COMPLETED", "text": "答案在此"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "prose" in html
    assert "答案在此" in html


def test_a_code_fence_in_the_prose_is_highlighted(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="TurnFinished", span="turn",
             data={"outcome": {"status": "TurnStatus.COMPLETED",
                               "text": "```python\nx = 1\n```"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "tok-num" in html


# --------------------------------------------------------------- no nesting


def test_the_index_page_is_one_valid_document(tmp_path):
    """R9c put a whole ``<!doctype>…`` inside ``<body>``; R20b must not."""
    store = _session(tmp_path)
    view = read_session_view(store, "sess-1")
    report = aggregate_sessions((SessionRow("sess-1", turns=1, files=1, prompt_tokens=10),))
    html = render_index_html(report, [view])
    assert html.count("<!doctype") == 1
    assert html.count("<html") == 1
    assert html.count("<style>") == 1
    # The per-session fragment carries no document wrapper of its own.
    assert html.index("</style>") < html.index("session sess-1")


# -------------------------------------------------------------------- theme


def test_the_theme_follows_the_system_and_can_be_forced(tmp_path):
    """No JavaScript: the force switches are checkboxes, which is what keeps the
    no-``<script>`` rule (and therefore tool output) safe."""
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert "prefers-color-scheme" in html
    assert "<script" not in html
    assert "force-light" in html and "force-dark" in html


def test_the_forced_theme_overrides_the_body_not_the_page(tmp_path):
    """R20b: the override used to land on ``.page``.  ``body`` resolves its own
    ``color`` from the variables one level up, so forcing light left every
    inherited colour light-on-white — the page was unreadable."""
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert "body:has(#force-light:checked)" in html
    assert "body:has(#force-light:checked) .page" not in html
    # The two switches are mutually exclusive by rule, not by source order.
    assert ":not(:has(#force-dark:checked))" in html


def test_the_palette_matches_the_tui_tokens(tmp_path):
    """The export must be the product, not a debug dump."""
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert "#0a0a0a" in html      # backgroundBase dark
    assert "#9d7cd8" in html      # hueAccent dark
    assert "#e06c75" in html      # feedbackError dark


# ------------------------------------------------------------- accessibility


def test_the_page_uses_semantic_landmarks(tmp_path):
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert "<header" in html and "<main" in html and "<footer" in html


def test_the_theme_switch_is_keyboard_reachable(tmp_path):
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert 'for="force-light"' in html
    assert ":focus-visible" in html


def test_badges_carry_text_not_colour_alone(tmp_path):
    """Colour-blind users cannot read a badge that is only a hue."""
    store = _session(tmp_path, rows=[
        _row(evt="ToolFinished", span="c1", parent="i1",
             data={"call_id": "c1", "tool_name": "read", "success": True}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert ">ok<" in html


# ----------------------------------------------------------------- security


def test_a_script_in_the_model_prose_is_escaped(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="TurnFinished", span="turn",
             data={"outcome": {"status": "TurnStatus.COMPLETED",
                               "text": "<script>alert(1)</script>"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_a_script_in_a_degradation_is_escaped(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="Degraded", span="i1",
             data={"component": "<img src=x onerror=1>", "reason": "<b>r</b>"}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "<img" not in html and "<b>r</b>" not in html


def test_no_external_resources(tmp_path):
    html = render_session_html(read_session_view(_session(tmp_path), "sess-1"))
    assert "http://" not in html and "https://" not in html
    assert "<link" not in html


def test_rendering_is_deterministic(tmp_path):
    store = _session(tmp_path)
    view = read_session_view(store, "sess-1")
    assert render_session_html(view) == render_session_html(view)


def test_an_empty_session_renders_without_raising(tmp_path):
    """A turn file that exists but recorded nothing is a crashed turn, not a
    missing one: the card stays, honestly labelled ``unknown``."""
    html = render_session_html(read_session_view(_session(tmp_path, rows=[]), "sess-1"))
    assert "kpi" in html
    assert "turn 1" in html
    assert "unknown" in html
