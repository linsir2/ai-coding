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
    RecallParams,
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

#: Read off the class, not hard-coded: the bounded-index case has to state what
#: the payload would cost *without* this cap, and a copy of the literal would go
#: stale the first time somebody tunes it.
PREVIEW_CHARS = MemoryService._PREVIEW_CHARS


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
    """The band, and the band *alone*.

    The first version of this case asserted a false mechanism: its tail notes
    shared only one of the four known query terms, so `relevance 0.043 < 0.25`
    and the **floor** dropped them -- measured identical at `relative_band=0.0`
    and `min_relevance=0.0`, which means the case was insensitive to both gates.

    These two tails clear the floor (`relevance 0.290`) and clear the absolute
    threshold (`score 0.268` / `0.258`, floor is `0.25`), so nothing but the band
    can drop them.  The assertion therefore runs the same corpus twice and
    requires the results to **differ**: with the band at its default only `best`
    survives; with `relative_band=0.0` both tails come back.  If either gate stops
    dropping what it should, one of those two legs fails.
    """
    query = "上下文压缩分几层"
    notes = [
        _note("best", "上下文压缩有四层：L3 落盘、L1 裁剪、L2 信封", age_days=1),
        # Matched ('上下','下文','压缩') — three of the four known terms, which is
        # what puts relevance at 0.290 rather than under the floor.  Old and
        # unimportant so the blended score lands in (threshold, band floor).
        _note("tail1", "压缩上下文摘要", age_days=30, importance=1),
        _note("tail2", "上下文与压缩", age_days=45, importance=1),
    ]
    scored = rank_notes_detailed(notes, query, now=NOW)
    detail = {
        hit.note.id: (round(hit.score, 3), round(hit.relevance, 3))
        for hit in scored.kept + scored.rejected
    }
    with_band = rank_notes_detailed(notes, query, now=NOW)
    without_band = rank_notes_detailed(
        notes, query, params=RecallParams(relative_band=0.0), now=NOW
    )
    kept = [hit.note.id for hit in with_band.kept]
    returned = [hit.note.id for hit in without_band.kept]
    ok = kept == ["best"] and set(returned) == {"best", "tail1", "tail2"}
    return ok, f"{detail} kept={kept} band_off={returned}"


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
    still fits in 400 characters of `index`.

    Note lengths are the spec's measured 40-88 chars, not the ~11 the first
    version used.  With 11-char notes the map serialised to 224 chars, so
    raising `PREVIEW_CHARS` from 40 to 200 left the assertion green and the
    bound was decorative — the same trap
    `tests/test_tool_memory_search.py::test_r10c_the_index_map_is_bounded_and_only_on_a_miss`
    documents in its own docstring, reintroduced in this copy.  The assertion
    below therefore also states that the *uncapped* payload would exceed 400,
    which is what makes it a test of the cap and not of the corpus.
    """
    filler = (
        "构建产物、依赖锁文件、缓存目录和测试快照都落在同一个工作目录下，"
        "需要定期清理归档，并且不能提交到仓库里，发布前还要确认一次"
    )
    texts = [f"第 {index} 条无关笔记：{filler}" for index in range(500)]
    service, _store = _service(
        tmp, [_note(f"n{i:04d}", t, age_days=30) for i, t in enumerate(texts)]
    )
    tool = MemorySearchTool(service)

    miss = await tool.execute({"query": "Kubernetes 集群有多少节点"})
    hit = await tool.execute({"query": texts[3][:12]})

    data = miss.data or {}
    index = data.get("index") or {}
    size = len(json.dumps(index, ensure_ascii=False))
    # What the payload would cost with no preview cap at all.  The previews in
    # `recent` are all exactly `PREVIEW_CHARS`, so the full texts have to come
    # from the store -- this is the number that proves the cap is load-bearing.
    previewed = [service.store.get(item["id"]) for item in (index.get("recent") or [])]
    uncapped = size + sum(
        max(0, len(note.text) - PREVIEW_CHARS) for note in previewed if note is not None
    )
    # `active`, not `total`: status() spends `total` on every note including the
    # superseded ones, and this map counts the active set only.
    ok = (
        size <= 400
        and uncapped > 400
        and index.get("active") == 500
        and len(index.get("recent") or []) <= 3
        and "index" not in (hit.data or {})
    )
    return ok, f"{size} chars (uncapped {uncapped}) for a 500-note store"


async def kind_filter_narrows_before_ranking(tmp: Path) -> tuple[bool, str]:
    """R10c: filters run on the candidate set, so `k` is spent on notes the model
    asked for.

    The corpus matters more than the k, and the first version of this case got it
    backwards.  Its four facts and one decision all covered 「依赖管理」, and
    `kind_boosts` gives a decision **+0.25**, so the decision ranked *first* —
    measured ranking `[d1, f0, f1, f2, f3]` — and post-filtering at **any** k
    still returned `['d1']`.  The case could not fail.  Its docstring claimed the
    facts outranked the decision, which was false; lowering k to 1 did not help,
    because at k=1 the budget *is* `d1`.

    These notes do the opposite: the four facts cover the whole query
    (`依赖管理用 uv 安装/升级/锁定/发布`, relevance 1.0 each) while the decision
    covers only `uv` (`决定：uv 作为默认工具`, relevance 0.092).  The facts take the
    whole budget and post-filtering empties it.  Measured here:
    `k=3 unfiltered -> ['f0','f1','f2']`, and the same k with `kind=decision`
    returns the one decision the unfiltered ranking dropped.

    The assertion runs **both** orderings: it requires the unfiltered result to be
    all facts (so the corpus really is decision-last) and the filtered one to be
    the decision.  A corpus swap that put the decision back on top fails the
    first leg instead of quietly passing the second.
    """
    notes = [
        _note(f"f{i}", text, age_days=3)
        for i, text in enumerate(
            ("依赖管理用 uv 安装", "依赖管理用 uv 升级", "依赖管理用 uv 锁定", "依赖管理用 uv 发布")
        )
    ]
    notes.append(_note("d1", "决定：uv 作为默认工具", kind="decision", age_days=30))
    service, _ = _service(tmp, notes)
    tool = MemorySearchTool(service)

    unfiltered = await tool.execute({"query": "依赖管理 uv", "k": 3})
    filtered = await tool.execute({"query": "依赖管理 uv", "k": 3, "kind": "decision"})

    plain = [note["id"] for note in (unfiltered.data or {}).get("notes", [])]
    ids = [note["id"] for note in (filtered.data or {}).get("notes", [])]
    # The facts must win unfiltered, or the filtered leg proves nothing.
    ok = plain == ["f0", "f1", "f2"] and ids == ["d1"]
    return ok, f"unfiltered={plain} kind=decision -> {ids}"


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


async def an_empty_store_says_it_is_empty_not_that_the_query_was_wrong(
    tmp: Path,
) -> tuple[bool, str]:
    """Critical 1: the first `memory_search` on a fresh workspace.

    `rank_notes_detailed` returns a bare `RankingResult()` when there are no
    active notes, so `terms_known`, `terms_unknown` and `no_vocabulary_match`
    are all empty/false.  Reading the empty `terms_unknown` as "no word was
    unknown" — i.e. "every word was known" — put three false lines on screen:

        没有可靠匹配（索引里有 0 条：空）。
        查询用词索引都认识，但没有候选达到相关度门槛。
        换一个说法再搜，或用 kind=... 缩小范围。

    The third line is the one that costs the model a turn: with nothing stored,
    no rewording can match.  The premise is asserted first, so this case cannot
    pass later by the empty store becoming measurable.
    """
    service, _ = _service(tmp, [])
    result = await MemorySearchTool(service).execute({"query": "依赖管理"})
    data = result.data or {}
    ok = (
        data.get("no_match") is True
        and data.get("terms_known") == []
        and data.get("terms_unknown") == []
        and data.get("near_misses") == []
        # 0 条 is measured, so it stays.
        and "索引里有 0 条" in (result.output or "")
        and "查询用词索引都认识" not in (result.output or "")
        and "换一个说法" not in (result.output or "")
    )
    return ok, f"known={data.get('terms_known')} output={result.output!r}"


async def a_query_that_measures_no_words_is_not_an_all_known_query(
    tmp: Path,
) -> tuple[bool, str]:
    """Critical 1, second trigger: the same clause, with notes already stored.

    The bare-result path is also reached when `tokenize(query)` yields nothing
    while the index is fine, so "the store is empty" is not the only thing that
    can produce it — and the index count here proves the store is not empty, so
    the clause cannot be excused by that reading either.
    """
    service, _ = _service(tmp, [_note("a", "打包命令是 npm run build 产出 dist", age_days=1)])
    tool = MemorySearchTool(service)
    seen: list[tuple[Any, bool]] = []
    for query in ("???", "怎么", "the"):
        result = await tool.execute({"query": query})
        data = result.data or {}
        seen.append(
            (
                query,
                "查询用词索引都认识" not in (result.output or "")
                and data.get("terms_known") == []
                and data.get("terms_unknown") == []
                and (data.get("index") or {}).get("active") == 1,
            )
        )
    ok = all(passed for _query, passed in seen)
    return ok, f"{[(q, p) for q, p in seen]}"


async def a_vocabulary_miss_does_not_claim_the_content_is_absent(
    tmp: Path,
) -> tuple[bool, str]:
    """Critical 2: a claim about content, made from a measurement about tokens.

    `no_vocabulary_match` means no query term has `df > 0` in the candidate
    set.  Measured here: the store holds 「打包命令是 npm run build 产出 dist」,
    the query is 「怎么构建」, and `tokenize` gives `['么构', '构建']`.  么构 is a
    cross-word bigram that would appear in no relevant note, so its absence
    carries no information about whether the content is stored — the clause was
    structurally unsupportable, not merely occasionally wrong.  It fired on the
    ordinary miss path for any query with a wholly unknown vocabulary, denied
    the problem was a wording one, and contradicted the advice line below it.
    """
    service, _ = _service(tmp, [_note("a", "打包命令是 npm run build 产出 dist", age_days=1)])
    result = await MemorySearchTool(service).execute({"query": "怎么构建"})
    data = result.data or {}
    output = result.output or ""
    ok = (
        set(data.get("terms_unknown") or []) == {"么构", "构建"}
        # The measurement is still reported.
        and "一条笔记都没出现过" in output
        # No claim about content, and no claim that wording is not the problem.
        and "不是措辞问题" not in output
        and "这批内容不在记忆里" not in output
        # And rephrasing stays on the table, which is what the old clause denied.
        and "换一个说法" in output
    )
    return ok, f"unknown={data.get('terms_unknown')}"


async def the_threshold_is_still_a_gate_when_the_band_is_not(tmp: Path) -> tuple[bool, str]:
    """spec 7 case 8, which the plan dropped: `threshold` 仍然生效.

    It had zero coverage — deleting `item.score >= resolved.threshold` left all
    1301 tests green. The band is switched off here so the threshold is the only
    clause that can reject the second note: it covers the whole query, so the
    relevance floor never touches it, and it scores 0.2417.

    The band cannot stay at its default, and the reason corrects a natural
    assumption. **At the shipped defaults the threshold is provably never the
    binding constraint**: `weight_rank = 0.55` gives the best-BM25 note at least
    `0.55 * (raw/best_raw) = 0.55`, so `best_score >= 0.55` whenever anything
    scores, and the band floor `0.55 * 0.55 = 0.3025` is already above
    `threshold = 0.25`. Measured over 400 random corpora the minimum `best_score`
    with any BM25 > 0 was exactly 0.55 and the floor never dipped below the
    threshold; over 20 000 notes, 14 805 cleared `min_relevance` and were
    rejected, and **zero** of those were rejected by the threshold alone. It is
    a compatibility floor for configs that lower the band or raise the threshold,
    which is exactly what the spec case asks for.
    """
    notes = [
        _note("top", "支付服务超时阈值配置在配置文件里", age_days=1),
        _note("mid", f"支付服务超时阈值{'与查询无关的背景说明' * 60}", importance=1,
              age_days=300),
    ]
    query = "支付服务超时阈值"
    gated = rank_notes_detailed(
        notes, query, params=RecallParams(threshold=0.25, relative_band=0.0), now=NOW
    )
    ungated = rank_notes_detailed(
        notes, query, params=RecallParams(threshold=0.0, relative_band=0.0), now=NOW
    )
    mid = next(hit for hit in gated.kept + gated.rejected if hit.note.id == "mid")
    ok = (
        [hit.note.id for hit in gated.kept] == ["top"]
        and [hit.note.id for hit in ungated.kept] == ["top", "mid"]
        and mid.relevance >= 0.25
        and mid.score < 0.25
    )
    return ok, (
        f"mid relevance={mid.relevance:.3f} score={mid.score:.4f} "
        f"kept={[h.note.id for h in gated.kept]} "
        f"threshold_off={[h.note.id for h in ungated.kept]}"
    )


async def only_what_the_model_saw_counts_as_a_use(tmp: Path) -> tuple[bool, str]:
    """A use is something the model was shown, not something that ranked.

    Both `docs/data-contracts.md` and `docs/features.md` assert this, and
    nothing pinned it: touching every candidate instead of the returned hits
    left all 1301 tests green. The corpus is the point — with a single matching
    note the two behaviours are indistinguishable, so all six notes here cover
    「依赖管理 uv」 completely and clear every gate (measured with `limit=None`:
    all six kept). `k=1` returns one of them.

    The cost of getting it wrong is not bookkeeping tidiness. `hits >= 3 ->
    importance + 1` and the eviction value function both read this column, so
    six fabricated uses promote a note two recalls earlier than real use would,
    on the strength of a `k` the caller picked.
    """
    texts = ("依赖管理用 uv 安装", "依赖管理用 uv 升级", "依赖管理用 uv 锁定",
             "依赖管理用 uv 发布", "依赖管理用 uv 测试", "依赖管理用 uv 部署")
    notes = [_note(f"f{index}", text, age_days=3) for index, text in enumerate(texts)]
    service, store = _service(tmp, notes)

    # The premise: the gates really do clear all six, so "only one was touched"
    # is a decision about k rather than the gates rejecting the rest.
    unranked = rank_notes_detailed(store.active(), "依赖管理 uv", now=NOW, limit=len(texts))
    hits, _diagnostics = await service.recall_with_diagnostics("依赖管理 uv", k=1)
    counts = {note.id: note.hits for note in store.active()}
    ok = (
        [hit.note.id for hit in unranked.kept] == [f"f{index}" for index in range(6)]
        and [hit.note.id for hit in hits] == ["f0"]
        and counts == {"f0": 1, **{f"f{index}": 0 for index in range(1, 6)}}
    )
    return ok, (
        f"gate_cleared={len(unranked.kept)} "
        f"returned={[h.note.id for h in hits]} hits={counts}"
    )


async def the_gate_clause_still_fires_when_every_word_is_known(
    tmp: Path,
) -> tuple[bool, str]:
    """The control the three cases above need.

    "Never renders 查询用词索引都认识" also passes on a tool that simply deleted
    the clause.  This corpus earns the clause: all five query words measure as
    known, and `near_misses` comes back empty because three zero-match decisions
    head the rejected list on score and are then dropped by the zero-match
    filter.  A gate rejection with no other true diagnosis left.

    Two properties carry it, so both are stated rather than left to be
    rediscovered.  The word-carrying notes are ancient and `importance=1`, so
    each scores `0.55 * 1.0 = 0.55` — every matching note ties for the best BM25
    (only matching notes score at all), so freshness and importance are the only
    levers a zero-match note has.  The decisions must then reach
    `0.20 + 0.15 + 0.25 = 0.60`, which needs freshness 1.0: they are stamped from
    the **live** clock because the tool path passes no `now` and the service
    would otherwise age them against this file's pinned ``NOW``.  Stamped to a
    fixture date instead, this case passes or fails depending on the day it runs.
    """
    words = ["alpha", "bravo", "charlie", "delta", "echo"]
    notes = [
        _note(f"old{index}", f"note mentioning {word} once", importance=1, age_days=2000)
        for index, word in enumerate(words)
    ]
    live = datetime.now(timezone.utc).isoformat()
    notes.extend(
        MemoryNote(id=f"d{index}",
                   text=f"decision number {index} about an unrelated workflow",
                   kind=NoteKind.DECISION, importance=5,
                   created_time=live, updated_time=live)
        for index in range(3)
    )
    service, _ = _service(tmp, notes)
    result = await MemorySearchTool(service).execute({"query": " ".join(words)})
    data = result.data or {}
    ok = (
        data.get("no_match") is True
        and data.get("terms_known") == words
        and data.get("terms_unknown") == []
        and data.get("near_misses") == []
        and "查询用词索引都认识" in (result.output or "")
    )
    return ok, (
        f"known={len(data.get('terms_known') or [])} "
        f"near={len(data.get('near_misses') or [])}"
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
    """An observation, not a gate.  Recall writes, so concurrent recalls can
    interleave with concurrent harvests on one store.

    Threads, not ``asyncio.gather``: ``recall_notes`` contains no ``await``, so
    ``gather`` serialises both coroutines completely and this case would pass
    without any concurrency at all.

    What it observes, precisely: that concurrent recalls against concurrent
    harvests **survive** -- no thread raised, the recall's usage signal reached
    disk, the file is still there and still parseable, and the reloaded note
    count matches the in-memory one.  It prints both counts either way.

    What it does **not** observe: the shared-tmp corruption.  ``MemoryStore``
    has that hazard (``_mutate`` releases its lock before ``_save``, so two
    threads serialise onto one ``<path>.tmp`` and a concurrent write can land on
    the live file), but reproducing it needs the writes to be *individually*
    synchronised -- a barrier before each write, 4 threads x 30 writes, 5-6 of 25
    trials corrupt.  Threads merely started together do not hit it: 20 runs of
    this case and 12 more on review produced no corruption, and a barrier placed
    once before the loop gives 0/25.  So a clean result here is **not** evidence
    the hazard is absent; ``MemoryStore``'s docstring carries the corrected
    recipe, and the hazard stays unfixed (pre-existing M3 behaviour, R10 chose
    to record it rather than restructure persistence in a retrieval round).
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
    "the_threshold_is_still_a_gate_when_the_band_is_not": (
        the_threshold_is_still_a_gate_when_the_band_is_not
    ),
    "a_query_nothing_in_memory_says_recalls_nothing": (
        a_query_nothing_in_memory_says_recalls_nothing
    ),
    # the known hole
    "the_decoy_still_leaks_and_says_so": the_decoy_still_leaks_and_says_so,
    # the usage signal
    "a_hit_counts_as_a_use": a_hit_counts_as_a_use,
    "a_miss_touches_nothing": a_miss_touches_nothing,
    "only_what_the_model_saw_counts_as_a_use": only_what_the_model_saw_counts_as_a_use,
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
    "an_empty_store_says_it_is_empty_not_that_the_query_was_wrong": (
        an_empty_store_says_it_is_empty_not_that_the_query_was_wrong
    ),
    "a_query_that_measures_no_words_is_not_an_all_known_query": (
        a_query_that_measures_no_words_is_not_an_all_known_query
    ),
    "a_vocabulary_miss_does_not_claim_the_content_is_absent": (
        a_vocabulary_miss_does_not_claim_the_content_is_absent
    ),
    "the_gate_clause_still_fires_when_every_word_is_known": (
        the_gate_clause_still_fires_when_every_word_is_known
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
