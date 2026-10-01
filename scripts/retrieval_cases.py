#!/usr/bin/env python3
"""R10 retrieval benchmark — our own memories, our own questions, real numbers.

    .venv/bin/python scripts/retrieval_cases.py
    .venv/bin/python scripts/retrieval_cases.py --only-paraphrase
    .venv/bin/python scripts/retrieval_cases.py --check      # gate, non-zero on regression

Offline and deterministic: the clock is pinned and nothing is read from a
config, so two runs on two days print the same table.  The guardrails are the
ones this round committed to; `--check` is what CI should call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone  # noqa: E402

from ai_coding.memory.benchmark import evaluate, format_metrics, load_corpus  # noqa: E402
from ai_coding.memory.relevance import RecallParams, rank_notes  # noqa: E402

#: Pinned so recency is part of the fixture, not of the day we run it.
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
CORPUS = ROOT / "scripts" / "retrieval_corpus.json"

#: What this round promised (docs/superpowers/plans/2026-10-01-r10-retrieval.md).
#: R@1 is deliberately ungated: it measures 0.675 today, and the 0.85 the spec
#: quotes belongs to its semantic tier, which this round does not build.  The
#: 0.725 in the plan is the figure this round is aiming at, not the one it
#: starts from — do not read it as the current number.
# FP@5 is gated at 0.35, not at today's 0.518: a "must not get worse" gate
# would pass on a round that never improved at all. The spec quotes 0.32;
# this is the same number re-measured on the shipped corpus, and it is left
# slightly loose so an unrelated ±1 note cannot turn the gate brittle.
GUARDRAILS = {"R@3": 0.80, "paraphrase@3": 6, "FP@5": 0.35}
CATEGORIES = ("lexical", "identifier", "paraphrase", "multi", "noanswer", "trap")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(CORPUS))
    parser.add_argument("--only-paraphrase", action="store_true",
                        help="print the paraphrase rows in full (they are the hard ones)")
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero when a guardrail is not met")
    args = parser.parse_args(argv)

    notes, cases = load_corpus(args.corpus, now=NOW)

    def rank(query: str) -> list[str]:
        return [
            hit.note.id
            for hit in rank_notes(notes, query, params=RecallParams(), now=NOW, limit=5)
        ]

    overall = evaluate(rank, cases)
    print(format_metrics("all", overall))
    for category in CATEGORIES:
        subset = [case for case in cases if case.category == category]
        if not subset:
            continue
        label = f"  {category}"
        print(format_metrics(label, evaluate(rank, subset)))
        if args.only_paraphrase and category == "paraphrase":
            rows = evaluate(rank, subset)
            for query in rows.missed:
                print(f"      missed: {query!r}")

    if overall.missed:
        print("\nmissed:", ", ".join(repr(q) for q in overall.missed))
    if overall.spurious:
        print("answered-but-unlabelled:", ", ".join(repr(q) for q in overall.spurious))

    para_cases = [c for c in cases if c.category == "paraphrase"]
    paraphrase = evaluate(rank, para_cases)
    para_at_3 = round(paraphrase.recall_at(3) * len(para_cases))
    print(
        f"\nguardrails: R@3>={GUARDRAILS['R@3']} "
        f"paraphrase@3>={GUARDRAILS['paraphrase@3']} FP@5<={GUARDRAILS['FP@5']}"
    )
    failures = []
    if overall.recall_at(3) < GUARDRAILS["R@3"]:
        failures.append(f"R@3={overall.recall_at(3):.2f}")
    if para_at_3 < GUARDRAILS["paraphrase@3"]:
        failures.append(f"paraphrase@3={para_at_3}")
    if overall.false_positive_share > GUARDRAILS["FP@5"]:
        failures.append(f"FP@5={overall.false_positive_share:.2f}")
    if failures:
        print("GUARDRAIL FAILED: " + ", ".join(failures))
        return 1 if args.check else 0
    print("guardrails OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
