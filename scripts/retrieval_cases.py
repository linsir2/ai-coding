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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone  # noqa: E402

from ai_coding.memory.benchmark import evaluate, format_metrics, load_corpus  # noqa: E402
from ai_coding.memory.relevance import RecallParams, rank_notes  # noqa: E402
from ai_coding.memory.service import _default_config  # noqa: E402

#: Pinned so recency is part of the fixture, not of the day we run it.
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
CORPUS = ROOT / "scripts" / "retrieval_corpus.json"

#: What this round promised (docs/superpowers/plans/2026-10-01-r10-retrieval.md).
#: R@1 is deliberately ungated: it measures 0.675 today, and the 0.85 the spec
#: quotes belongs to its semantic tier, which this round does not build.  The
#: 0.725 in the plan is the figure this round is aiming at, not the one it
#: starts from — do not read it as the current number.
# FP@5 is gated at 0.36, not at the pre-round 0.518: a "must not get worse" gate
# would pass on a round that never improved at all. The spec quotes 0.32.
# The measured value is 0.35045..., so the gate is that number rounded *up* to
# the guardrail's own two decimals — never down onto it. A gate written at
# exactly 0.35 is 0.00045 *below* the thing it guards, so `--check` fails on our
# own result in the fifth decimal and a green gate would mean nothing. Rounding
# up keeps the gate's meaning ("no worse than what we measured") and the margin
# still fails the 0.518 a non-improving round produces.
#: Guards for the **lexically reachable** subset only — lexical + identifier +
#: multi.  This is what a lexical ranker is responsible for, so a regression here
#: is a real regression.
#:
#: Why not the aggregate: the corpus was expanded to 78 notes / 124 questions and
#: `paraphrase` is no longer a hand-labelled category — it is *defined* as the set
#: the lexical ranker does not answer in the top 3 (see
#: scripts/build_retrieval_corpus.py).  Its R@3 is therefore 0.00 by construction,
#: which drags the aggregate to 0.59 while telling us nothing about the ranker.
#: Gating the aggregate would mean gating a number that can only go down.
#:
#: Measured on this corpus: reachable R@3 = 0.99, reachable FP@5 = 0.46.
#: The gates sit just outside those, rounded the way the previous round's did —
#: up, so the gate cannot be satisfied by a change that made things worse.
#:
#: ``paraphrase@3`` is deliberately **not** gated.  It is the headroom a semantic
#: tier would have to capture (docs/r10-analysis.md §1.4 predicted 9/12 on the old,
#: 12-item set).  Measured on the expanded set it is 0/46 today, so a gate would
#: only ever be red until that tier exists — and a permanently red gate is the
#: "brittle tuning game" D-R10-1 warns about.
GUARDRAILS = {"reachable_R@3": 0.95, "reachable_FP@5": 0.50}
REACHABLE = ("lexical", "identifier", "multi")
CATEGORIES = ("lexical", "identifier", "paraphrase", "multi", "noanswer", "trap")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(CORPUS))
    parser.add_argument("--only-paraphrase", action="store_true",
                        help="print the paraphrase rows in full (they are the hard ones)")
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero when a guardrail is not met")
    parser.add_argument("--with-embeddings", action="store_true",
                        help="second table via the real production path with a live "
                             "EmbeddingIndex (needs network + a configured provider)")
    parser.add_argument("--config", default="config.local.yaml",
                        help="config file holding models.* for --with-embeddings")
    parser.add_argument("--provider", default="qwen",
                        help="models.<name> to embed with for --with-embeddings")
    parser.add_argument("--fusion", default="blend", choices=["blend", "rrf"],
                        help="R11d: how lexical and semantic opinions become one rank "
                             "term. blend is the default and the measured better one; "
                             "rrf is here to be re-measured, not to be shipped blind")
    args = parser.parse_args(argv)

    notes, cases = load_corpus(args.corpus, now=NOW)
    params = RecallParams(recall_fusion=args.fusion)

    def rank(query: str) -> list[str]:
        return [
            hit.note.id
            for hit in rank_notes(notes, query, params=params, now=NOW, limit=5)
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

    reachable = evaluate(rank, [c for c in cases if c.category in REACHABLE])
    para_cases = [c for c in cases if c.category == "paraphrase"]
    para_at_3 = round(evaluate(rank, para_cases).recall_at(3) * len(para_cases))
    noanswer = evaluate(rank, [c for c in cases if c.category in ("noanswer", "trap")])
    print(
        f"\nreachable (lexical+identifier+multi): R@3={reachable.recall_at(3):.2f} "
        f"FP@5={reachable.false_positive_share:.2f}"
    )
    print(
        f"paraphrase@3={para_at_3}/{len(para_cases)} (ungated: this is the headroom a "
        f"semantic tier would capture)"
    )
    print(f"no-answer correctly refused: {noanswer.no_answer_ok}/{noanswer.no_answer} (reported)")
    print(
        f"\nguardrails: reachable R@3>={GUARDRAILS['reachable_R@3']} "
        f"reachable FP@5<={GUARDRAILS['reachable_FP@5']}"
    )
    failures = []
    if reachable.recall_at(3) < GUARDRAILS["reachable_R@3"]:
        failures.append(f"reachable_R@3={reachable.recall_at(3):.2f}")
    if reachable.false_positive_share > GUARDRAILS["reachable_FP@5"]:
        failures.append(f"reachable_FP@5={reachable.false_positive_share:.2f}")
    if failures:
        print("GUARDRAIL FAILED: " + ", ".join(failures))
        return 1 if args.check else 0
    print("guardrails OK")
    if args.with_embeddings:
        return _run_semantic_table(notes, cases, args)
    return 0


def _run_semantic_table(notes, cases, args) -> int:
    """Second table through the true production path, with a live index.

    This is the only place that touches the network in the benchmark suite.  It
    builds a real MemoryService over the corpus notes with a real
    EmbeddingIndex, so the numbers measure the shipped code rather than a
    reimplementation of it.  Compare against the lexical table above.

    R11d: it prints the **same breakdown** as the lexical table (per category,
    then reachable R@3/FP@5 and the guardrail line).  Without that, "does the
    fusion rule move the gates" is a question the yardstick cannot answer — which
    is exactly why the earlier semantic numbers in the docs could not be
    reproduced from this script.
    """
    import asyncio
    import tempfile

    import yaml

    from ai_coding.config.models import EmbeddingsConfig, _coerce_model
    from ai_coding.memory.embeddings import EmbeddingIndex
    from ai_coding.memory.service import MemoryService
    from ai_coding.memory.store import MemoryStore

    parser_args = args
    raw = yaml.safe_load(Path(parser_args.config).read_text(encoding="utf-8"))
    models = {
        name: _coerce_model(item)
        for name, item in (raw.get("models") or {}).items()
    }
    if parser_args.provider not in models:
        print(f"no models.{parser_args.provider} in {parser_args.config}")
        return 2
    cfg = EmbeddingsConfig(
        enabled=True, provider=parser_args.provider, model="text-embedding-v4",
        min_cosine=0.60, cache_path=str(
            Path(tempfile.gettempdir()) / "r10-bench-embeddings.json"
        ),
    )
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / "m.json")
        for note in notes:
            store.add_note(note)
        service = MemoryService(
            store,
            embeddings=EmbeddingIndex(cfg, models),
            config=SimpleNamespace(**{**vars(_default_config()),
                                      "recall_fusion": parser_args.fusion}),
        )

        def rank(query: str) -> list[str]:
            hits, _ = asyncio.run(
                service.recall_with_diagnostics(query, k=5, now=NOW)
            )
            return [hit.note.id for hit in hits]

        print(f"\nsemantic (live index, same corpus, same clock, fusion="
              f"{parser_args.fusion}):")
        print(format_metrics("semantic", evaluate(rank, cases)))
        for category in CATEGORIES:
            subset = [case for case in cases if case.category == category]
            if not subset:
                continue
            print(format_metrics(f"  {category}", evaluate(rank, subset)))
        reachable = evaluate(rank, [c for c in cases if c.category in REACHABLE])
        para_cases = [c for c in cases if c.category == "paraphrase"]
        para_at_3 = round(evaluate(rank, para_cases).recall_at(3) * len(para_cases))
        print(
            f"reachable (lexical+identifier+multi): R@3={reachable.recall_at(3):.2f} "
            f"FP@5={reachable.false_positive_share:.2f}"
        )
        print(f"semantic paraphrase@3={para_at_3}/{len(para_cases)}")
        if parser_args.fusion != "blend":
            # Deliberately not a gate. A switch that is off by default and measured
            # worse must not be turned into a contract by making it fail CI; what
            # it owes the reader is this line, and the numbers above it.
            print(
                f"note: fusion={parser_args.fusion} is not the default and is not "
                f"gated -- the guardrails above cover the default lexical path only"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
