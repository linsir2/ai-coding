"""TDD (R4a): turning a trace file into something a human can read.

The point of recording is being able to answer "why did that turn cost so
much / what degraded" without opening the JSONL by hand
(docs/r4-analysis.md 11).
"""

import json

from ai_coding.ui.trace_render import render_budget, render_turn, summarize_turn


def _write(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _rec(span, parent, evt, data, *, turn=1, iteration=1):
    return {
        "v": 1,
        "ts": "2026-01-01T00:00:00.000Z",
        "session": "s1",
        "turn": turn,
        "span": span,
        "parent": parent,
        "iter": iteration,
        "evt": evt,
        "data": data,
    }


def _turn_file(tmp_path):
    path = tmp_path / "traces" / "s1" / "turn-1.jsonl"
    _write(
        path,
        [
            _rec("turn", "", "TurnStarted", {}, iteration=0),
            _rec(
                "i1",
                "turn",
                "ContextAssembled",
                {"system_chars": 100, "tools_chars": 50},
            ),
            _rec(
                "step1",
                "i1",
                "ModelStepFinished",
                {
                    "usage": {
                        "prompt_tokens": 900,
                        "completion_tokens": 100,
                        "total_tokens": 1000,
                    },
                    "cached_tokens": 700,
                    "latency_ms": 1500,
                },
            ),
            _rec("c1", "i1", "ToolStarted", {"tool_name": "read"}),
            _rec(
                "c1",
                "i1",
                "ToolFinished",
                {"tool_name": "read", "success": True, "duration_ms": 120},
            ),
            _rec("i1", "turn", "Degraded", {"component": "memory_block", "reason": "boom"}),
        ],
    )
    return path


def test_summarize_turn_counts_what_matters(tmp_path):
    summary = summarize_turn(_turn_file(tmp_path))

    assert summary["turn"] == 1
    assert summary["spans"] == 4  # turn, i1, step1, c1
    assert summary["model_steps"] == 1
    assert summary["tool_calls"] == 1
    assert summary["prompt_tokens"] == 900
    assert summary["completion_tokens"] == 100
    assert summary["cached_tokens"] == 700
    assert summary["degraded"] == ["memory_block"]
    assert summary["tool_ms"] == 120
    assert summary["model_ms"] == 1500


def test_render_turn_is_human_readable(tmp_path):
    text = render_turn(summarize_turn(_turn_file(tmp_path)))

    assert "turn 1" in text
    assert "1,000 tok" in text or "1000 tok" in text
    assert "cache 78%" in text  # 700/900
    assert "memory_block" in text


def test_render_budget_reports_session_totals():
    text = render_budget(
        {
            "turns": 3,
            "steps": 5,
            "prompt_tokens": 10_000,
            "completion_tokens": 500,
            "cached_tokens": 8_000,
        }
    )
    assert "3 turns" in text
    assert "10,500" in text or "10500" in text
    assert "cache 80%" in text


def test_render_budget_handles_an_empty_summary():
    assert "no recorded usage" in render_budget({})


def test_summarize_turn_survives_a_missing_file(tmp_path):
    summary = summarize_turn(tmp_path / "nope.jsonl")
    assert summary["turn"] == 0
    assert summary["spans"] == 0
