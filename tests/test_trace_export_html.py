"""TDD (R9c): one self-contained HTML page per session — escaped, offline, stable."""

import json
from pathlib import Path

from ai_coding.core.aggregate import SessionRow, aggregate_sessions
from ai_coding.core.export_html import read_session_view, render_index_html, render_session_html
from ai_coding.core.trace import TraceStore


def _row(**over) -> dict:
    base = {
        "v": 1, "ts": "2026-09-28T00:00:00.000Z", "session": "sess-1", "turn": 1,
        "span": "i1", "parent": "turn", "iter": 1, "evt": "IterationStarted",
        "data": {},
    }
    base.update(over)
    return base


def _session(
    tmp_path: Path, *, session: str = "sess-1", rows: list[dict] | None = None
) -> TraceStore:
    store = TraceStore(tmp_path / "traces", workspace=tmp_path)
    writer = store.begin_turn(session)
    for row in rows if rows is not None else [_row()]:
        writer.write_line(json.dumps(row))
    (store.session_dir(session) / "summary.json").write_text(
        json.dumps({"turns": 1, "steps": 1, "prompt_tokens": 10,
                    "completion_tokens": 1, "cached_tokens": 5}),
        encoding="utf-8",
    )
    return store


def test_r9_script_in_a_tool_output_is_escaped(tmp_path):
    store = _session(tmp_path, rows=[
        _row(evt="ToolStarted", span="c1", parent="i1",
             data={"tool_name": "bash", "params": {"command": "echo '<script>alert(1)</script>'"}}),
    ])
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_r9_session_id_and_tool_names_are_escaped(tmp_path):
    store = _session(
        tmp_path,
        session="s<b>1",
        rows=[_row(evt="ToolStarted", span="c1", data={"tool_name": "<b>"})],
    )
    html = render_session_html(read_session_view(store, "s<b>1"))
    assert "&lt;b&gt;" in html


def test_r9_the_page_has_no_external_resources(tmp_path):
    store = _session(tmp_path)
    html = render_session_html(read_session_view(store, "sess-1"))
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html and "<link" not in html


def test_r9_full_payload_is_opt_in(tmp_path):
    store = _session(tmp_path)
    (store.session_dir("sess-1") / "turn-1.full.json").write_text(
        json.dumps({"messages": [{"role": "user", "content": "SECRET-TEXT"}]}),
        encoding="utf-8",
    )
    assert "SECRET-TEXT" not in render_session_html(read_session_view(store, "sess-1"))
    assert "SECRET-TEXT" in render_session_html(
        read_session_view(store, "sess-1", full=True)
    )


def test_r9_every_turn_becomes_a_details_block_with_its_summary(tmp_path):
    store = _session(tmp_path)
    view = read_session_view(store, "sess-1")
    html = render_session_html(view)
    assert html.count("<details>") == len(view.turns) == 1
    assert "turn 1" in html


def test_r9_rendering_is_deterministic(tmp_path):
    store = _session(tmp_path)
    view = read_session_view(store, "sess-1")
    assert render_session_html(view) == render_session_html(view)


def test_r9_the_index_page_carries_the_totals(tmp_path):
    store = _session(tmp_path, session="sess-1")
    views = [read_session_view(store, "sess-1")]
    report = aggregate_sessions((SessionRow("sess-1", turns=1, files=1, prompt_tokens=10),))
    html = render_index_html(report, views)
    assert "sess-1" in html and "TOTAL" in html.upper()


def test_r9_a_fifty_turn_session_stays_under_a_megabyte(tmp_path):
    store = TraceStore(tmp_path / "traces", workspace=tmp_path)
    for turn in range(1, 51):
        writer = store.begin_turn("big")
        for index in range(20):
            writer.write_line(
                json.dumps(
                    _row(
                        turn=turn,
                        span=f"c{index}",
                        data={"tool_name": "bash", "output_preview": "x" * 200},
                    )
                )
            )
    html = render_session_html(read_session_view(store, "big"))
    assert len(html.encode("utf-8")) < 1_000_000
