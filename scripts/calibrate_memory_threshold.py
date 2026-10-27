#!/usr/bin/env python3
"""M-F1 / A.2: calibrate ``min_cosine`` against the retrieval corpus.

``min_cosine`` is the absolute gate on raw cosines.  It is only ever justified by
measurement: the shipped 0.60 was measured on text-embedding-v4, and a different
embedding model needs a different number.  This script produces the curve.

It reuses ``scripts/retrieval_cases.py`` for corpus loading and for the semantic
service construction (``_build_semantic_service``), so the numbers measure the
shipped recall path rather than a reimplementation of it.  The transport is
injected, so the sweep runs offline and deterministically; pass
``--transport live`` to go through a real endpoint instead.

Effective domain: the recommended threshold is ``f(corpus, embedding model)``.
Changing either one requires re-running this script.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.memory.benchmark import evaluate, load_corpus  # noqa: E402
from ai_coding.memory.relevance import RecallParams, rank_notes  # noqa: E402
from scripts.retrieval_cases import (  # noqa: E402
    CORPUS,
    NOW,
    REACHABLE,
    _build_semantic_service,
)

#: Wide enough to see both ends: below the overlap floor the gate passes
#: everything, above it the semantic tier never fires.
THRESHOLDS = tuple(round(0.30 + 0.05 * step, 2) for step in range(13))

#: Injected-transport dimension.  Any value works because the injected config
#: sets ``dimensions=0`` ("take whatever the endpoint returns").
EMBED_DIM = 256
_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokens(text: str) -> list[str]:
    lowered = text.casefold()
    tokens = _WORD_RE.findall(lowered)
    for segment in _CJK_RE.findall(lowered):
        tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
        if len(segment) == 1:
            tokens.append(segment)
    return tokens


def _embed(text: str) -> list[float]:
    vector = [0.0] * EMBED_DIM
    for token in _tokens(text):
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % EMBED_DIM
        vector[index] += 1.0 if digest[2] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


async def _offline_transport(model: str, inputs: list[str]) -> list[list[float]]:
    """A deterministic bag-of-words embedding, so the sweep needs no network."""
    return [_embed(text) for text in inputs]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", default=str(CORPUS))
    parser.add_argument("--config", default="config.local.yaml")
    parser.add_argument("--provider", default="qwen")
    parser.add_argument(
        "--transport",
        choices=("offline", "live"),
        default="offline",
        help="offline injects a deterministic embedding; live reads models.<provider>",
    )
    args = parser.parse_args(argv)

    notes, cases = load_corpus(args.corpus, now=NOW)
    lexical_cases = [case for case in cases if case.relevant]
    reachable_cases = [case for case in lexical_cases if case.category in REACHABLE]
    relevant_of = {case.query: case.relevant for case in lexical_cases}
    params = RecallParams()

    def lexical_rank(query: str) -> list[str]:
        return [
            hit.note.id
            for hit in rank_notes(notes, query, params=params, now=NOW, limit=5)
        ]

    lexical_hit = {
        case.query
        for case in lexical_cases
        if set(lexical_rank(case.query)[:3]) & relevant_of[case.query]
    }
    paraphrase_missed = {
        case.query for case in lexical_cases if case.query not in lexical_hit
    }

    transport = None if args.transport == "live" else _offline_transport
    # A named directory rather than the helper's `mkdtemp`: that one is never
    # cleaned up, so every calibration run left an empty directory behind.
    with tempfile.TemporaryDirectory(prefix="ai-coding-calibrate-") as directory:
        return _sweep(
            notes, cases, lexical_cases, reachable_cases, relevant_of,
            lexical_rank, lexical_hit, paraphrase_missed, transport, args,
            Path(directory),
        )


def _sweep(
    notes, cases, lexical_cases, reachable_cases, relevant_of, lexical_rank,
    lexical_hit, paraphrase_missed, transport, args, directory,
) -> int:
    service = _build_semantic_service(
        notes,
        args,
        min_cosine=THRESHOLDS[0],
        transport=transport,
        directory=directory,
    )
    if service is None:
        return 2
    embeddings_cfg = service._config.embeddings  # noqa: SLF001 — calibration knob

    # FP is measured on the reachable subset, the same window the shipped
    # metric uses (features.md:504): the paraphrase set is a miss set, so its
    # FP@5 is 1.0 by construction and would swamp the signal.
    baseline_fp = evaluate(lexical_rank, reachable_cases).false_positive_share
    rows: list[tuple[float, int, int, float]] = []
    for threshold in THRESHOLDS:
        embeddings_cfg.min_cosine = threshold
        sem: dict[str, list[str]] = {}
        for case in lexical_cases:
            hits, _ = asyncio.run(
                service.recall_with_diagnostics(case.query, k=5, now=NOW)
            )
            sem[case.query] = [hit.note.id for hit in hits]
        recovered = sum(
            1 for query in paraphrase_missed if set(sem[query]) & relevant_of[query]
        )
        regression = sum(
            1 for query in lexical_hit if not (set(sem[query]) & relevant_of[query])
        )
        fp = evaluate(
            lambda query, _sem=sem: _sem[query], reachable_cases
        ).false_positive_share
        rows.append((threshold, recovered, regression, fp))

    print(f"corpus: {args.corpus}")
    print(f"queries: {len(cases)}  answerable: {len(lexical_cases)}  "
          f"unanswerable: {len(cases) - len(lexical_cases)} (excluded from FP)")
    print(f"lexical_hit={len(lexical_hit)}/{len(reachable_cases)} reachable  "
          f"paraphrase_missed={len(paraphrase_missed)}")
    print(f"baseline FP@5 (lexical, reachable subset) = {baseline_fp:.4f}")
    print()
    print("min_cosine  paraphrase_recovered  lexical_regression  FP@5")
    for threshold, recovered, regression, fp in rows:
        print(
            f"  {threshold:.2f}            {recovered:>3}/{len(paraphrase_missed):<3}"
            f"              {regression:>3}          {fp:.4f}"
        )
    print()
    feasible = [
        row for row in rows if row[2] == 0 and row[3] <= baseline_fp + 1e-9
    ]
    if feasible:
        best = max(feasible, key=lambda row: (row[1], -row[0]))
        print(
            f"recommended min_cosine = {best[0]:.2f}  "
            f"(paraphrase_recovered={best[1]}, lexical_regression={best[2]}, "
            f"FP@5={best[3]:.4f})"
        )
    else:
        print(
            "no threshold keeps lexical_regression==0 and FP@5 at or below baseline; "
            "read the curve and choose the FP you accept"
        )
    print()
    print("有效域 = f(语料, embedding 模型)：换语料或换 embedding 模型必须重跑本脚本重标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
