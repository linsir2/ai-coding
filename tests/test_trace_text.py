"""TDD (R4a): turning a trace file into something a human can read.

The point of recording is being able to answer "why did that turn cost so
much / what degraded" without opening the JSONL by hand
(docs/r4-analysis.md 11).
"""

import json

import pytest

from ai_coding.app.trace_text import render_budget, render_turn, summarize_turn


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


def test_budget_figures_report_totals_money_and_cache():
    """The sidebar's one-line budget reads the same numbers /budget renders.

    Extracted so the second consumer cannot grow a second cost formula: the
    split (fresh vs cached input) and the money math live in one place.
    """
    from ai_coding.app.trace_text import budget_figures
    from ai_coding.core.budget import Pricing

    plan = Pricing(input_per_1k=1.0, output_per_1k=2.0, cached_per_1k=0.1)
    figures = budget_figures(
        {"prompt_tokens": 2000, "completion_tokens": 500, "cached_tokens": 1000},
        price=plan,
    )
    assert figures["tokens"] == 2500
    assert figures["cost"] == pytest.approx(2.1)  # 1.0 fresh + 0.1 cached + 1.0 out
    assert figures["cachedPercent"] == 50


def test_budget_figures_leave_cost_none_without_a_price():
    from ai_coding.app.trace_text import budget_figures

    figures = budget_figures({"prompt_tokens": 10, "completion_tokens": 5})
    assert figures["tokens"] == 15
    assert figures["cost"] is None
    assert figures["cachedPercent"] is None


def test_budget_figures_sum_the_per_model_breakdown():
    from ai_coding.app.trace_text import budget_figures
    from ai_coding.core.budget import Pricing

    prices = {
        "cheap": Pricing(input_per_1k=1.0, output_per_1k=1.0),
        "pricey": Pricing(input_per_1k=2.0, output_per_1k=2.0),
    }
    figures = budget_figures(
        {
            "prompt_tokens": 2000,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "by_model": {
                "cheap": {"prompt_tokens": 1000, "completion_tokens": 0, "cached_tokens": 0},
                "pricey": {"prompt_tokens": 1000, "completion_tokens": 0, "cached_tokens": 0},
            },
        },
        prices=prices,
    )
    assert figures["cost"] == pytest.approx(3.0)  # 1.0 + 2.0
    assert figures["cachedPercent"] is None


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


def _compaction_turn_file(tmp_path):
    """One turn whose history was rewritten, with a per-layer account (M-E3)."""
    path = tmp_path / "traces" / "s1" / "turn-3.jsonl"
    _write(
        path,
        [
            _rec("turn", "", "TurnStarted", {}, iteration=0),
            _rec(
                "i1",
                "turn",
                "CompactionStarted",
                {"reason": "auto"},
            ),
            _rec(
                "i1",
                "turn",
                "CompactionEnded",
                {
                    "reason": "auto",
                    "beforeMessages": 74,
                    "afterMessages": 3,
                    "inputTokens": 1312,
                    "outputTokens": 147,
                    "totalTokens": 1896,
                    "text": "## 目标\ndone",
                    "layers": [
                        {
                            "name": "L3",
                            "removed": 0,
                            "rewritten": 1,
                            "skipped": 2,
                            "before_tokens": 90000,
                            "after_tokens": 80000,
                        },
                        {
                            "name": "L1",
                            "removed": 20,
                            "rewritten": 0,
                            "skipped": 0,
                            "before_tokens": 80000,
                            "after_tokens": 60000,
                        },
                        {
                            "name": "L2",
                            "removed": 0,
                            "rewritten": 5,
                            "skipped": 3,
                            "before_tokens": 60000,
                            "after_tokens": 40000,
                        },
                        {
                            "name": "L4",
                            "removed": 0,
                            "rewritten": 0,
                            "skipped": 0,
                            "before_tokens": 40000,
                            "after_tokens": 20000,
                        },
                    ],
                    "estimated_before": 90000,
                    "estimated_after": 20000,
                },
            ),
        ],
    )
    return path


def test_the_trace_renders_the_compaction_layers(tmp_path):
    """W6: /trace renders the per-layer account, by token delta.

    A turn that compacted must say what each layer did; the counts alone would
    mislead for L2/L3, so the line leads with the token transition.
    """
    text = render_turn(summarize_turn(_compaction_turn_file(tmp_path)))

    assert "压缩：" in text
    assert "L1 −20 条" in text
    assert "L2 改写 5 条" in text
    assert "跳过 2 条" in text
    assert "L4 摘要 40000→20000 token（估）" in text


def test_the_trace_says_nothing_about_compaction_when_there_were_none(tmp_path):
    """No compaction, no line: the common case must not grow a zero-valued tail."""
    text = render_turn(summarize_turn(_turn_file(tmp_path)))

    assert "压缩：" not in text


@pytest.mark.asyncio
async def test_a_real_compaction_event_renders_its_token_deltas(tmp_path):
    """End to end: the JSONL is snake_case (``dataclasses.asdict``), not the
    wire's camelCase.

    A reader that looked for ``beforeTokens`` would render ``0→0`` on a real
    trace — the layers are there, the numbers are not.  This runs the real
    recorder and compactor, so the key names cannot drift apart again.
    """
    from types import SimpleNamespace

    from ai_coding.core.compaction import Compactor
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.core.events import TurnFinished
    from ai_coding.core.trace import TraceRecorder, TraceStore
    from ai_coding.domain.loop import TurnOutcome, TurnStatus
    from ai_coding.domain.message import ChatMessage, ToolCallRef

    store = TraceStore(tmp_path / "traces", workspace=tmp_path)
    recorder = TraceRecorder(store, "s1")

    async def summarizer(text, previous=None, on_delta=None, usage_sink=None):
        return "## 目标\n摘要"

    settings = SimpleNamespace(
        max_context_tokens=12_000,
        max_messages=6,
        snip_keep_head=3,
        snip_keep_tail=3,
        keep_recent_tool_results=3,
        per_result_persist_bytes=5_000,
        l4_tail_tokens=15_000,
    )
    context = ContextManager(settings, transcripts_root=tmp_path, summarizer=summarizer)
    compactor = Compactor(context, emit=recorder.on_event)
    history = [ChatMessage(role="user", content="z" * 40_000, timestamp="")]
    for index in range(8):
        history.append(
            ChatMessage(
                role="assistant",
                content=None,
                timestamp="",
                tool_calls=[ToolCallRef(id=f"c{index}", name="bash", arguments="{}")],
            )
        )
        history.append(
            ChatMessage(
                role="tool",
                content="y" * 5_000,
                timestamp="",
                tool_call_id=f"c{index}",
                tool_name="bash",
            )
        )
    await compactor.compress(history, deadline=None, force=True)
    recorder.on_event(
        TurnFinished(
            outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="ok", iterations=1, reason="")
        )
    )

    text = render_turn(summarize_turn(recorder.last_path))
    assert "压缩：" in text
    # The token deltas are real numbers, not the 0→0 a camelCase reader gives.
    assert "0→0 token" not in text
    assert "L1 −10 条" in text


# ---- G7: /budget splits the spend by model ----------------------------------


def test_budget_splits_by_model_once_more_than_one_was_used():
    from ai_coding.core.budget import Pricing

    text = render_budget(
        {
            "turns": 1,
            "steps": 3,
            "prompt_tokens": 15_000,
            "completion_tokens": 900,
            "by_model": {
                "qwen": {
                    "prompt_tokens": 10_000,
                    "completion_tokens": 500,
                    "cached_tokens": 0,
                    "steps": 2,
                },
                "deepseek": {
                    "prompt_tokens": 5_000,
                    "completion_tokens": 400,
                    "cached_tokens": 0,
                    "steps": 1,
                },
            },
        },
        prices={
            "qwen": Pricing(input_per_1k=0.002, output_per_1k=0.008),
            "deepseek": Pricing(input_per_1k=0.001, output_per_1k=0.004),
        },
    )

    assert "qwen" in text
    assert "deepseek" in text
    # qwen: 10 * 0.002 + 0.5 * 0.008 = 0.024 ; deepseek: 5 * 0.001 + 0.4 * 0.004 = 0.0066
    assert "0.0240" in text
    assert "0.0066" in text


def test_budget_stays_one_line_for_a_single_model():
    """A per-model breakdown of one model is the total, repeated."""
    from ai_coding.core.budget import Pricing

    text = render_budget(
        {
            "turns": 1,
            "steps": 2,
            "prompt_tokens": 1_000,
            "completion_tokens": 100,
            "by_model": {
                "qwen": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 100,
                    "cached_tokens": 0,
                    "steps": 2,
                }
            },
        },
        prices={"qwen": Pricing(input_per_1k=0.002, output_per_1k=0.008)},
    )

    assert text.count("\n") == 0


def test_an_unpriced_model_shows_tokens_and_no_estimate():
    """R4a's rule, per model: an unknown price yields no cost, never a fake
    zero, and the tokens are still reported."""
    from ai_coding.core.budget import Pricing

    text = render_budget(
        {
            "turns": 1,
            "steps": 2,
            "prompt_tokens": 6_000,
            "completion_tokens": 300,
            "by_model": {
                "qwen": {
                    "prompt_tokens": 5_000,
                    "completion_tokens": 200,
                    "cached_tokens": 0,
                    "steps": 1,
                },
                "mystery": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 100,
                    "cached_tokens": 0,
                    "steps": 1,
                },
            },
        },
        prices={"qwen": Pricing(input_per_1k=0.002, output_per_1k=0.008)},
    )

    mystery = next(line for line in text.splitlines() if "mystery" in line)
    assert "1,000" in mystery or "1000" in mystery
    assert "≈" not in mystery
    # The session total is still priced, but only from the models that can be.
    assert "≈" in text.splitlines()[0]


def test_a_summary_without_by_model_renders_as_before():
    """Every summary written before this change has no per-model split, and the
    line has to stay exactly what it was."""
    text = render_budget({"turns": 1, "steps": 1, "prompt_tokens": 10, "completion_tokens": 1})

    assert text.count("\n") == 0


def test_the_price_map_is_keyed_by_the_wire_name(tmp_path):
    """The trap the first version fell into: a config key need not equal the
    name sent to the provider (``qwen`` -> ``qwen3.8-2.4t-a95b``), and the step
    reports the wire name.  Keying the prices by the config key produced a
    summary with tokens and no money -- silently, because an unknown price is
    supposed to yield nothing.
    """
    from ai_coding.cli import _make_recorder
    from ai_coding.config.models import AIConfig, AppConfig
    from ai_coding.domain.model_config import ModelConfig

    cfg = AppConfig(
        default_model="qwen",
        ai=AIConfig(),
        models={
            "qwen": ModelConfig(
                name="qwen3.8-2.4t-a95b",
                base_url="https://a/v1",
                api_key="k",
                price_input_per_1k=0.01,
            ),
        },
    )
    cfg.tracing.enabled = True

    recorder = _make_recorder(cfg, str(tmp_path), session=None)

    assert recorder is not None
    assert list(recorder.pricings) == ["qwen3.8-2.4t-a95b"]
