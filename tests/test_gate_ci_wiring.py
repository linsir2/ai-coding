"""M-G1: the two local-only gates must stay runnable under the arguments CI uses.

A step that silently stops failing is worse than no step: ``--check`` defaults
off, so ``main([])`` exits 0 no matter what the guardrails say.  These pin the
*CI* invocations, so weakening a guardrail constant fails here too, not only in
a workflow file nobody runs locally.

The other gates named by the roadmap are already in CI (``memory_cases``,
``trace_cases``, ``redline_benchmark``), and ``r7_cases`` / ``r11c_cases`` are
offline gates that are red today -- they are deliberately not wired here.
"""

from __future__ import annotations

import asyncio


def test_the_retrieval_guardrail_gate_passes():
    from scripts import retrieval_cases

    assert retrieval_cases.main(["--check"]) == 0


def test_the_r18_acceptance_gate_passes_offline():
    """``main`` is a coroutine; comparing it to 0 would always be false."""
    from scripts import r18_acceptance

    assert asyncio.run(r18_acceptance.main(live=False)) == 0


def test_the_retrieval_guardrails_were_not_loosened():
    """The thresholds are what this gate protects, not a knob to turn.

    A red build is meant to be fixed by fixing the retriever.  Lowering the floor
    (or raising the FP ceiling) would turn the gate green without changing
    anything a user sees.
    """
    from scripts.retrieval_cases import GUARDRAILS

    assert GUARDRAILS["reachable_R@3"] >= 0.95
    assert GUARDRAILS["reachable_FP@5"] <= 0.50
