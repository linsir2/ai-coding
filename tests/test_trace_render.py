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


def _rec(span, parent, evt, data, *, turn=1, iteration=1, scope=""):
    row = {
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
    # The recorder only writes the key when there is a scope (trace.py).
    if scope:
        row["scope"] = scope
    return row


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


def test_budget_shows_an_estimate_when_prices_are_configured():
    """Money appears only when the model config knows its prices.

    ``CostLedger.cost`` had no production caller, so cached tokens were not
    merely mispriced — nobody was charged anything at all.  With a cache price
    configured the estimate splits the input into fresh and cached, because the
    two are billed differently; without prices the line stays absent, per R4a's
    "an unknown price must yield no cost rather than a fake 0.0".
    """
    from ai_coding.core.budget import BudgetLedger, Pricing

    ledger = BudgetLedger()
    ledger.record_step({"prompt_tokens": 10_000, "completion_tokens": 2_000,
                        "total_tokens": 12_000, "cached_tokens": 6_000})

    priced = render_budget(ledger.usage_dict(),
                           price=Pricing(input_per_1k=0.002, output_per_1k=0.008,
                                         cached_per_1k=0.0002))
    # 4k fresh * 0.002 + 6k cached * 0.0002 + 2k out * 0.008 = 0.0252
    assert "≈" in priced
    assert "0.0252" in priced

    unpriced = render_budget(ledger.usage_dict())
    assert "≈" not in unpriced
    assert "cache 60%" in unpriced


def _usage(prompt, completion=0):
    return {
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "cached_tokens": 0,
        "latency_ms": 10,
    }


def _delegating_turn_file(tmp_path):
    """One parent step and one child's, the shape a sub-agent turn produces."""
    path = tmp_path / "traces" / "s1" / "turn-7.jsonl"
    _write(
        path,
        [
            _rec("step1", "i1", "ModelStepFinished", _usage(900), iteration=1),
            _rec(
                "child-1",
                "call_1",
                "ModelStepFinished",
                _usage(300),
                iteration=1,
                scope="subagent:child:call_1",
            ),
        ],
    )
    return path


def test_summarize_turn_splits_out_the_subagent_share(tmp_path):
    """/trace reported a turn that was mostly sub-agent work as the parent's own.

    On one real turn `render_turn` said "41 model steps · 2,349,647 in" while
    35 of those steps and 2,088,510 of those tokens belonged to five children.
    The total is what the turn actually cost and must not change — the point is
    to see which slice of it was not the parent's.
    """
    summary = summarize_turn(_delegating_turn_file(tmp_path))

    assert summary["subagent_scopes"] == 1
    assert summary["subagent_model_steps"] == 1
    assert summary["subagent_prompt_tokens"] == 300
    # the total still covers both, so nothing downstream has to be re-derived
    assert summary["model_steps"] == 2
    assert summary["prompt_tokens"] == 1200


def test_summarize_turn_counts_each_subagent_scope_once(tmp_path):
    path = tmp_path / "traces" / "s1" / "turn-8.jsonl"
    _write(
        path,
        [
            _rec("a", "c1", "ModelStepFinished", _usage(100), scope="subagent:a:c1"),
            _rec("b", "c1", "ModelStepFinished", _usage(200), scope="subagent:a:c1"),
            _rec("c", "c2", "ModelStepFinished", _usage(400), scope="subagent:b:c2"),
        ],
    )
    summary = summarize_turn(path)

    assert summary["subagent_scopes"] == 2
    assert summary["subagent_model_steps"] == 3
    assert summary["subagent_prompt_tokens"] == 700


def test_render_turn_names_the_subagent_share(tmp_path):
    text = render_turn(summarize_turn(_delegating_turn_file(tmp_path)))

    assert "subagents 1" in text
    assert "1/2 steps" in text
    assert "300/1,200" in text


def test_render_turn_says_nothing_about_subagents_when_there_were_none(tmp_path):
    """No sub-agent, no line: the common case must not grow a zero-valued tail."""
    text = render_turn(summarize_turn(_turn_file(tmp_path)))

    assert "subagent" not in text
