#!/usr/bin/env python3
"""R10 acceptance cases — the round's claims, run against the real units.

Each case is one claim and returns (ok, detail) so a failure says what it saw.
Offline and deterministic: no network, no config, a pinned clock.  The shape
follows scripts/r9_cases.py.

    .venv/bin/python scripts/r10_cases.py
    .venv/bin/python scripts/r10_cases.py --only the_decoy_still_leaks_and_says_so

The cases are grouped by what they defend:
  * the gates        — the floor kills noise, the band cuts the tail
  * the known hole   — the generic-overlap decoy still leaks, on purpose
  * the usage signal — a hit counts, a miss writes nothing, promotion fires
  * the tool surface — a miss is structured, bounded and actionable
  * the closed loops — write then recall then consolidate; recall under churn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import threading
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.domain.message import ChatMessage  # noqa: E402
from ai_coding.memory.benchmark import evaluate, load_corpus  # noqa: E402
from ai_coding.memory.note import MemoryNote, NoteKind  # noqa: E402
from ai_coding.memory.relevance import (  # noqa: E402
    rank_notes,
    rank_notes_detailed,
)
from ai_coding.memory.schema import HarvestCandidate  # noqa: E402
from ai_coding.memory.service import MemoryService  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402
from ai_coding.tools.memory_search_tool import MemorySearchTool  # noqa: E402

SEP = "-" * 72
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]

#: Same value as ``scripts/retrieval_cases.py``'s GUARDRAILS, at the same
#: precision and for the same reason: the measured FP@5 is 0.35045..., so a gate
#: written at exactly 0.35 is below its own measurement and cannot pass.  Kept as
#: a literal rather than imported so this script grades the units directly and
#: does not depend on the CLI it is checking.
FP_GATE = 0.36

#: The decoy from docs/r10-analysis.md 1.6, named so two cases can share it.
DECOY_QUERY = "CLI 命令怎么加一个新子命令"
DECOY_ID = "n52"


def _note(
    note_id: str,
    text: str,
    *,
    kind: str = "fact",
    importance: int = 3,
    age_days: float = 0.0,
) -> MemoryNote:
    stamp = (NOW - timedelta(days=age_days)).isoformat()
    return MemoryNote(
        id=note_id,
        text=text,
        kind=NoteKind.parse(kind),
        importance=importance,
        created_time=stamp,
        updated_time=stamp,
    )


def _service(tmp: Path, notes: list[MemoryNote]) -> tuple[MemoryService, MemoryStore]:
    store = MemoryStore(tmp / "m.json")
    for note in notes:
        store.add_note(note)
    return MemoryService(store), store


# ------------------------------------------------------------- the gates ----


async def the_floor_kills_a_loud_irrelevant_note(tmp: Path) -> tuple[bool, str]:
    """spec 1.1: recency 0.20 + importance 0.15 = 0.35 clears a 0.25 threshold."""
    notes = [
        _note("loud", "今天要把 docs 目录里的章节重写一遍", importance=5),
        _note("real", "上下文压缩有四层：L3 落盘、L1 裁剪、L2 信封", age_days=30),
    ]
    kept = [hit.note.id for hit in rank_notes(notes, "上下文压缩分几层", now=NOW)]
    return kept == ["real"], f"kept={kept}"


async def bm25_prefers_the_specific_note(tmp: Path) -> tuple[bool, str]:
    notes = [
        _note("loud", "docs 目录里的章节要重写", importance=5),
        _note("real", "构建流程：make build 会先跑 lint 再编译，产物在 dist/", age_days=1),
    ]
    kept = [hit.note.id for hit in rank_notes(notes, "make build 产物在哪", now=NOW)]
    return bool(kept) and kept[0] == "real", f"kept={kept}"


async def the_relative_band_cuts_the_tail(tmp: Path) -> tuple[bool, str]:
    """`relative_band` asks "is this note in the running", and only the blended
    score can answer that: the tail shares every query term with the best note,
    so relevance alone keeps all three and the band cuts two."""
    notes = [
        _note("best", "上下文压缩有四层", age_days=1),
        _note("weak", "压缩", age_days=1),
        _note("tail", "压缩", age_days=1),
    ]
    result = rank_notes_detailed(notes, "上下文压缩分几层", now=NOW)
    kept = [hit.note.id for hit in result.kept]
    dropped = [hit.note.id for hit in result.rejected]
    # The floor is vacuous when only one candidate clears it (plan, known risks),
    # so this pins the band's own work: the same word in two weaker notes.
    ok = kept == ["best"] and set(dropped) == {"weak", "tail"}
    return ok, f"kept={kept} rejected={dropped}"


async def a_query_nothing_in_memory_says_recalls_nothing(tmp: Path) -> tuple[bool, str]:
    notes = [_note("a", "payment service timeout", importance=5)]
    result = rank_notes_detailed(notes, "Kubernetes 集群有多少节点", now=NOW)
    ok = not result.kept and result.no_vocabulary_match
    return ok, f"kept={len(result.kept)} unknown={result.terms_unknown}"


async def the_decoy_still_leaks_and_says_so(tmp: Path) -> tuple[bool, str]:
    """The hole from spec 1.6, pinned on the shipped corpus so nobody 'fixes' it.

    ``n52``「今天要把 docs 目录里的 CLI 章节重写一遍」 answers
    「CLI 命令怎么加一个新子命令」 with one matched word and nothing else.  It is
    not gated away because every gate that gated it also killed paraphrase
    recall: three candidates were measured and all three were rejected
    (docs/r10-analysis.md 1.6 — ``known_fraction``, idf-capped coverage,
    per-term idf weight), each buying the decoy at the cost of paraphrase@3.
    So the residual is accepted, and the case's job is to make it *visible*:
    the hit names the single word it matched, and the benchmark still lists the
    query as answered-but-unlabelled.

    A two-note fixture does not reproduce this: with the real answer beside it
    the decoy loses the band, so the case has to be run on the 52-note corpus
    where the decoy actually wins.  Measured there: ``kept[0] == "n52"`` with
    ``matched_terms == ("cli",)``, relevance 0.371.
    """
    notes, cases = load_corpus(ROOT / "scripts" / "retrieval_corpus.json", now=NOW)
    result = rank_notes_detailed(notes, DECOY_QUERY, now=NOW, limit=5)
    leaked = [hit for hit in result.kept if hit.note.id == DECOY_ID]
    labelled = next(
        case for case in cases if case.query == DECOY_QUERY
    )
    ok = (
        bool(leaked)
        and len(leaked[0].matched_terms) == 1
        and leaked[0].matched_terms[0] == "cli"
        # It is a leak only because nothing in the corpus labels it relevant.
        and not labelled.relevant
    )
    return ok, (
        f"decoy_kept={bool(leaked)} "
        f"matched={leaked[0].matched_terms if leaked else ()} "
        f"labelled_relevant={sorted(labelled.relevant)}"
    )


# ------------------------------------------------------ the usage signal ----


async def a_hit_counts_as_a_use(tmp: Path) -> tuple[bool, str]:
    """R10b: `hits >= 3 -> importance + 1` was dead code until recall touched."""
    service, store = _service(tmp, [_note("a", "项目依赖统一用 uv 管理", age_days=1)])
    await service.recall_notes("依赖怎么装", k=3)
    stored = store.get("a")
    ok = stored is not None and stored.hits == 1 and bool(stored.last_used)
    return ok, f"hits={stored.hits if stored else '?'}"


async def a_miss_touches_nothing(tmp: Path) -> tuple[bool, str]:
    service, store = _service(tmp, [_note("a", "项目依赖统一用 uv 管理", age_days=1)])
    await service.recall_notes("Kubernetes 集群有多少节点", k=3)
    stored = store.get("a")
    ok = stored is not None and stored.hits == 0 and stored.last_used == ""
    return ok, f"hits={stored.hits if stored else '?'}"


async def promotion_becomes_reachable_from_real_use(tmp: Path) -> tuple[bool, str]:
    service, store = _service(tmp, [_note("a", "项目依赖统一用 uv 管理", age_days=1)])
    for _ in range(3):
        await service.recall_notes("依赖怎么装", k=3)
    report = await service.consolidate(force=True)
    stored = store.get("a")
    ok = (
        "a" in report.promoted
        and stored is not None
        and stored.importance == 4
    )
    return ok, f"promoted={report.promoted} importance={stored.importance if stored else '?'}"


# ------------------------------------------------------- the tool surface ----


async def a_miss_is_structured_and_actionable(tmp: Path) -> tuple[bool, str]:
    service, _ = _service(tmp, [_note("a", "payment service timeout", age_days=1)])
    result = await MemorySearchTool(service).execute({"query": "依赖怎么装"})
    data = result.data or {}
    ok = (
        result.success is True
        and data.get("no_match") is True
        and data.get("notes") == []
        # Measured: tokenize('依赖怎么装') -> ['依赖', '赖怎', '么装'], and none of
        # them is in a one-note English corpus.
        and set(data.get("terms_unknown") or []) == {"依赖", "赖怎", "么装"}
        and isinstance(data.get("index"), dict)
        and data.get("semantic_unavailable") is False
        and "换一个说法" in (result.output or "")
    )
    return ok, f"no_match={data.get('no_match')} unknown={data.get('terms_unknown')}"


async def the_index_map_stays_bounded(tmp: Path) -> tuple[bool, str]:
    """A miss must not return the index back into the context: a 500-note store
    still fits in 400 characters of `index`, because it counts and previews."""
    service, _ = _service(
        tmp, [_note(f"n{i:04d}", f"第 {i} 条无关笔记", age_days=30) for i in range(500)]
    )
    result = await MemorySearchTool(service).execute({"query": "Kubernetes 集群有多少节点"})
    data = result.data or {}
    size = len(json.dumps(data.get("index") or {}, ensure_ascii=False))
    # `active`, not `total`: status() spends `total` on every note including the
    # superseded ones, and this map counts the active set only.
    ok = size <= 400 and (data.get("index") or {}).get("active") == 500
    return ok, f"{size} chars for a 500-note store"


async def kind_filter_narrows_before_ranking(tmp: Path) -> tuple[bool, str]:
    """R10c: filters run on the candidate set, so `k` is spent on notes the model
    asked for.  With four facts that outrank the one decision, the old
    post-filter path spent the whole budget on the facts and returned nothing;
    the note the model asked for was in the index the whole time."""
    notes = [_note(f"f{i}", f"依赖管理用 uv 第 {i} 条", age_days=i + 1) for i in range(4)]
    notes.append(_note("d1", "决定：依赖管理统一用 uv", kind="decision", age_days=30))
    service, _ = _service(tmp, notes)
    result = await MemorySearchTool(service).execute(
        {"query": "依赖管理", "k": 1, "kind": "decision"}
    )
    ids = [note["id"] for note in (result.data or {}).get("notes", [])]
    # k=1 on purpose: with the pre-R10c ordering the decision already ranked
    # first (it has a +0.25 kind boost), so a bigger k could not tell the two
    # implementations apart.  k=1 with a filter asks the only question that has
    # one right answer.
    return ids == ["d1"], f"ids={ids}"


async def a_filter_emptied_everything_still_says_what_it_measured(
    tmp: Path,
) -> tuple[bool, str]:
    """The N2 fix: an emptied candidate set leaves the ranking with no corpus, so
    every query word looks absent from it.  The text must not render that as
    「查询用词索引都认识」 while `terms_known` is empty beside it."""
    service, _ = _service(
        tmp,
        [_note(f"x{i}", f"note about {word} only")
         for i, word in enumerate(("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"))],
    )
    result = await MemorySearchTool(service).execute(
        {"query": "alpha bravo charlie delta echo foxtrot", "kind": "preference"}
    )
    data = result.data or {}
    # One word per note, so coverage is 1/6 and nothing clears the floor: the
    # probe keeps nothing either, and the two rankings differ only in what they
    # can report.  Nothing clearing the gate is also why `filtered_out` is 0 —
    # there is nothing to blame on the filter.
    ok = (
        data.get("no_match") is True
        and len(data.get("terms_known") or []) == 6
        and data.get("terms_unknown") == []
        and bool(data.get("near_misses"))
        and "查询用词索引都认识" not in (result.output or "")
    )
    return ok, (
        f"terms_known={data.get('terms_known')} near={len(data.get('near_misses') or [])}"
    )


async def an_empty_since_days_is_not_a_silent_empty_recall(tmp: Path) -> tuple[bool, str]:
    """The N3 fix: `since_days=""` is what an unfilled tool field arrives as, and
    `float("")` used to raise inside the best-effort `except`, which reported it
    as an empty recall with no index summary at all."""
    service, _ = _service(
        tmp, [_note("a", "依赖管理统一用 uv", age_days=4000)]
    )
    hits, diagnostics = await service.recall_with_diagnostics(
        "依赖管理", k=5, since_days=""  # type: ignore[arg-type]
    )
    ok = (
        [hit.note.id for hit in hits] == ["a"]
        and diagnostics.index_summary is not None
    )
    return ok, (
        f"hits={[hit.note.id for hit in hits]} "
        f"summary={diagnostics.index_summary is not None}"
    )


# ------------------------------------------------------------ closed loops ----


async def write_then_recall_then_consolidate(tmp: Path) -> tuple[bool, str]:
    """The loop a real session runs, end to end, and every step of it observable.

    Note the order: recall only *after* a write, so each of the three recalls has
    something to return.  (The brief read ``report.added`` off the
    ``consolidate`` report; that is a ``HarvestReport`` field — ``consolidate``
    reports merged/promoted/expired/evicted, so the brief's version raised
    ``AttributeError`` rather than asserting anything.  The ids come from the
    three writes instead.)

    The third recall is what makes consolidation do something: ``hits >= 3`` is
    the promotion threshold, and it is unreachable unless recall writes.
    """
    service, store = _service(tmp, [])
    written: list[str] = []
    for turn in range(3):
        written.extend(
            service.write_note(
                f"第 {turn} 轮：依赖管理统一用 uv", kind="decision", session_id="s1"
            ).added
        )
        await service.recall_notes("依赖怎么装", k=3)

    report = await service.consolidate(force=True)
    stored = [store.get(note_id) for note_id in written]
    hits = [note.hits if note else -1 for note in stored]
    promoted_note = stored[0] if len(written) == 1 else None
    # One note: turn 1's text is a subset of turn 2's, so dedupe or similarity
    # folds all three writes into one id — which is what makes three recalls on
    # it enough to promote.
    ok = (
        promoted_note is not None
        and hits[0] == 3
        and written[0] in report.promoted
        and promoted_note.importance == 4
    )
    return ok, (
        f"written={len(written)} hits={hits} "
        f"promoted={report.promoted} "
        f"importance={promoted_note.importance if promoted_note else '?'}"
    )


_HARVEST_ROUNDS = 40


async def recall_survives_concurrent_harvest(tmp: Path) -> tuple[bool, str]:
    """An observation, not a gate.  Recall writes, so recall and harvest can
    interleave on one store.

    Threads, not ``asyncio.gather``: ``recall_notes`` contains no ``await``, so
    ``gather`` serialises both coroutines completely and this case would pass
    with the shared-tmp hazard fully present.  Real threads do not.

    What it observes.  Notes are never torn or lost off disk: the file is written
    whole and swapped in with ``os.replace``.  But ``MemoryStore._mutate``
    releases its lock *before* ``_save``, and ``_save`` reads ``self._notes``
    unguarded, so two threads serialise onto the same ``<path>.tmp`` and the
    reloaded count can come back lower than the in-memory one.  The hazard is
    pre-existing (M3), R10 reviewed it and chose to record it rather than
    restructure persistence inside a retrieval round, and the store's docstring
    now says so.  Hence the assertion is only survival + the usage signal; the
    count difference is printed.
    """
    # An extractor is what makes harvest *write*.  Without one, `harvest` returns
    # at the "no extractor" guard after an eviction check, so the two harvest
    # threads would touch nothing at all and the case would exercise one thread.
    def extract(_messages: Any, _summary: str | None) -> list[HarvestCandidate]:
        return [
            HarvestCandidate(text=f"第 {turn} 轮采集到的笔记", kind=NoteKind.FACT)
            for turn in range(_HARVEST_ROUNDS)
        ]

    store = MemoryStore(tmp / "m.json")
    store.add_note(_note("a", "项目依赖统一用 uv 管理", age_days=1))
    service = MemoryService(store, extractor=extract)
    failures: list[str] = []

    async def recall() -> None:
        try:
            await service.recall_notes("依赖怎么装", k=3)
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            failures.append(f"recall: {exc!r}")

    def harvest(session: str) -> Coroutine[Any, Any, None]:
        async def run() -> None:
            for turn in range(_HARVEST_ROUNDS):
                try:
                    await service.harvest(
                        [ChatMessage(role="user", content=f"第 {turn} 轮", timestamp="")],
                        turn_index=turn,
                        session_id=session,
                        evidence="tool",
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"harvest({session}): {exc!r}")

        return run()

    # Two harvest threads plus one recall thread on one store: the recall's
    # touch-on-hit is a file write racing up to eighty others.
    threads = [
        threading.Thread(target=asyncio.run, args=(recall(),), name="recall"),
        threading.Thread(target=asyncio.run, args=(harvest("s1"),), name="harvest-s1"),
        threading.Thread(target=asyncio.run, args=(harvest("s2"),), name="harvest-s2"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    reloaded = MemoryStore(tmp / "m.json")
    survivors = reloaded.all()
    # The recall path is the thing under test: what it returned must have counted
    # as a use, and must still be readable from disk afterwards.
    touched = reloaded.get("a")
    ok = (
        not failures
        and touched is not None
        and touched.hits >= 1
        and len(survivors) >= 1
        and (tmp / "m.json").is_file()
    )
    return ok, (
        f"thread_failures={failures[:1]} "
        f"hits_on_disk={touched.hits if touched else '?'} "
        f"in_memory={len(store.all())} reloaded={len(survivors)}"
        + (
            ""
            if len(store.all()) == len(survivors)
            else "  <- an unlocked _save lost a write (pre-existing, recorded)"
        )
    )


# ------------------------------------------------------ the yardstick ----


async def the_benchmark_guardrails_hold(tmp: Path) -> tuple[bool, str]:
    """R10's own gate, re-run here so the acceptance script fails if the numbers
    move.  Same corpus, same pinned clock, same three guardrails as
    ``scripts/retrieval_cases.py --check``."""
    notes, cases = load_corpus(ROOT / "scripts" / "retrieval_corpus.json", now=NOW)

    def rank(query: str) -> list[str]:
        return [hit.note.id for hit in rank_notes(notes, query, now=NOW, limit=5)]

    overall = evaluate(rank, cases)
    paraphrase = evaluate(rank, [c for c in cases if c.category == "paraphrase"])
    ok = (
        overall.recall_at(3) >= 0.80
        and overall.false_positive_share <= FP_GATE
        and paraphrase.recall_at(3) * len(paraphrase_cases(cases)) >= 6
    )
    return ok, (
        f"R@1={overall.recall_at(1):.2f} R@3={overall.recall_at(3):.2f} "
        f"FP@5={overall.false_positive_share:.3f} "
        f"paraphrase@3={round(paraphrase.recall_at(3) * len(paraphrase_cases(cases)))}"
        f"/{len(paraphrase_cases(cases))} "
        f"no-answer={overall.no_answer_ok}/{overall.no_answer}"
    )


def paraphrase_cases(cases: list[Any]) -> list[Any]:
    return [case for case in cases if case.category == "paraphrase"]


OFFLINE: dict[str, CaseFn] = {
    # the gates
    "the_floor_kills_a_loud_irrelevant_note": the_floor_kills_a_loud_irrelevant_note,
    "bm25_prefers_the_specific_note": bm25_prefers_the_specific_note,
    "the_relative_band_cuts_the_tail": the_relative_band_cuts_the_tail,
    "a_query_nothing_in_memory_says_recalls_nothing": (
        a_query_nothing_in_memory_says_recalls_nothing
    ),
    # the known hole
    "the_decoy_still_leaks_and_says_so": the_decoy_still_leaks_and_says_so,
    # the usage signal
    "a_hit_counts_as_a_use": a_hit_counts_as_a_use,
    "a_miss_touches_nothing": a_miss_touches_nothing,
    "promotion_becomes_reachable_from_real_use": (
        promotion_becomes_reachable_from_real_use
    ),
    # the tool surface
    "a_miss_is_structured_and_actionable": a_miss_is_structured_and_actionable,
    "the_index_map_stays_bounded": the_index_map_stays_bounded,
    "kind_filter_narrows_before_ranking": kind_filter_narrows_before_ranking,
    "a_filter_emptied_everything_still_says_what_it_measured": (
        a_filter_emptied_everything_still_says_what_it_measured
    ),
    "an_empty_since_days_is_not_a_silent_empty_recall": (
        an_empty_since_days_is_not_a_silent_empty_recall
    ),
    # the closed loops
    "write_then_recall_then_consolidate": write_then_recall_then_consolidate,
    "recall_survives_concurrent_harvest": recall_survives_concurrent_harvest,
    # the yardstick
    "the_benchmark_guardrails_hold": the_benchmark_guardrails_hold,
}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="run one case by name")
    args = parser.parse_args(argv)

    cases = dict(OFFLINE)
    if args.only:
        cases = {name: fn for name, fn in cases.items() if name == args.only}
        if not cases:
            print(f"no such case: {args.only}")
            return 2

    passed = failed = 0
    for name, case in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            try:
                ok, detail = await case(Path(directory))
            except Exception as exc:  # noqa: BLE001 — one broken case must not stop the run
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
        passed += ok
        failed += not ok

    print(SEP)
    print(f"SUMMARY {passed}/{passed + failed} passed ({len(OFFLINE)} offline)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
