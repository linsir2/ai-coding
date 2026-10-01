"""TDD (R3a): recall scoring — relevance, recency, importance, kind, threshold.

Covers cases 2, 9 and 10 of docs/r3-analysis.md, plus the two flaws the prototype
found in this very design:

* G1: recency and importance alone could push an *irrelevant* note past the score
  threshold (0.2 + 0.075 = 0.275 > 0.25), so relevance needs its own hard gate;
* G2: exact-match dedupe missed a "记住：" prefix, so merging uses similarity.

Relevance is idf-weighted **coverage**, not normalised BM25: measured on case 9,
BM25's length bonus pushed a short low-information note to ``relevance=1.0`` and it
beat the longer note that actually answered the question.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ai_coding.memory.note import MemoryNote, NoteKind
from ai_coding.memory.relevance import (
    RecallParams,
    bm25_scores,
    coverage,
    rank_notes,
    rank_notes_detailed,
    recency,
)
from ai_coding.memory.tokenize import tokenize

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _note(note_id: str, text: str, **over) -> MemoryNote:
    age_days = over.pop("age_days", 0.0)
    created = (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    return MemoryNote(id=note_id, text=text, created_time=created, **over)


def test_coverage_rewards_the_document_that_shares_rare_terms():
    docs = [tokenize("public holiday schedule"), tokenize("build failed, see build.log")]
    query = tokenize("where is build.log")
    df = {"build.log": 1, "build": 1, "where": 2}
    strong = coverage(query, docs[1], df=df, n_docs=2)
    weak = coverage(query, docs[0], df=df, n_docs=2)
    assert strong > weak
    assert 0.0 <= weak <= strong <= 1.0


def test_coverage_ignores_terms_the_corpus_does_not_know():
    """CJK bigram noise ("怎么" from "依赖怎么装") must not dilute a real match."""
    note = tokenize("这个项目用 uv 管理依赖")
    query = tokenize("依赖怎么装")
    df = dict.fromkeys(set(note), 1)
    assert coverage(query, note, df=df, n_docs=1) == pytest.approx(1.0)


def test_recency_halves_every_half_life():
    assert recency("2026-09-23T12:00:00Z", now=NOW, half_life_days=14) == pytest.approx(1.0)
    assert recency("2026-09-09T12:00:00Z", now=NOW, half_life_days=14) == pytest.approx(0.5)
    assert recency("2026-08-26T12:00:00Z", now=NOW, half_life_days=14) == pytest.approx(0.25)
    # an unparseable timestamp must not punish the note
    assert recency("", now=NOW, half_life_days=14) == 1.0


def test_case_1_chinese_query_is_recalled():
    notes = [_note("n1", "这个项目用 uv 管理依赖，不要用 pip")]
    ranked = rank_notes(notes, "帮我给这个项目加一个依赖", params=RecallParams(), now=NOW)
    assert [r.note.id for r in ranked] == ["n1"]
    assert ranked[0].score >= 0.25


def test_case_2_newest_of_equally_relevant_notes_wins():
    notes = [
        _note("alpha", "alpha uses dependency X", age_days=10),
        _note("beta", "beta unrelated note", age_days=9),
        _note("gamma", "gamma uses dependency Y", age_days=8),
        _note("epsilon", "epsilon uses dependency Z", age_days=2),
    ]
    ranked = rank_notes(notes, "how do I add a dependency", params=RecallParams(), now=NOW)
    assert [r.note.id for r in ranked][:3] == ["epsilon", "gamma", "alpha"]
    assert "beta" not in [r.note.id for r in ranked]


def test_case_9_the_informative_note_wins_with_k1():
    notes = [
        _note("n1", "build 相关：构建脚本用 make", age_days=9),
        _note("n2", "测试相关：pytest 跑全量用例", age_days=8),
        _note(
            "n3",
            "构建流程：make build 会先跑 lint 再编译，产物在 dist/，失败看 build.log",
            age_days=1,
        ),
    ]
    ranked = rank_notes(
        notes, "make build 失败了", params=RecallParams(), now=NOW, limit=1
    )
    assert [r.note.id for r in ranked] == ["n3"]


def test_kind_boost_ranks_decisions_higher():
    notes = [
        _note("fact", "项目用 uv 管理依赖", kind=NoteKind.FACT),
        _note("decision", "项目用 uv 管理依赖", kind=NoteKind.DECISION),
    ]
    ranked = rank_notes(notes, "依赖怎么装", params=RecallParams(), now=NOW)
    assert [r.note.id for r in ranked] == ["decision", "fact"]


def test_case_10_irrelevant_notes_are_not_recalled():
    notes = [
        _note("pay", "payment service timeout is configured in config/pay.yaml"),
        _note("auth", "the auth service signs tokens with RS256"),
    ]
    ranked = rank_notes(
        notes, "please look at the file in the user module", params=RecallParams(), now=NOW
    )
    assert ranked == []


def test_g1_relevance_gate_blocks_a_new_important_but_irrelevant_note():
    """Prototype G1: 0.2·recency + 0.15·importance = 0.275 used to clear 0.25."""
    notes = [_note("fresh", "payment service timeout config", importance=5, pinned=True)]
    params = RecallParams()
    assert params.weight_recency + params.weight_importance > params.threshold
    assert rank_notes(notes, "how do I add a dependency", params=params, now=NOW) == []


def test_superseded_notes_are_never_recalled():
    replaced = _note("old", "项目用 uv 管理依赖").superseded_by_note(
        _note("new", "项目改用 poetry 管理依赖")
    )
    ranked = rank_notes([replaced], "依赖怎么装", params=RecallParams(), now=NOW)
    assert ranked == []


def test_ordering_is_deterministic_for_ties():
    notes = [_note("b", "项目用 uv 管理依赖"), _note("a", "项目用 uv 管理依赖")]
    ids = [r.note.id for r in rank_notes(notes, "依赖", params=RecallParams(), now=NOW)]
    assert ids == ["a", "b"]


def test_limit_is_respected():
    notes = [_note(f"n{i}", f"项目 {i} 用 uv 管理依赖") for i in range(10)]
    ranked = rank_notes(notes, "uv 依赖", params=RecallParams(), now=NOW, limit=2)
    assert len(ranked) == 2


def test_empty_query_recalls_nothing():
    notes = [_note("n1", "项目用 uv 管理依赖")]
    assert rank_notes(notes, "", params=RecallParams(), now=NOW) == []


# ---- R8b: evidence is a tie-breaker, nothing more (docs/r8-analysis.md 3.2) ----


def _ev_note(note_id: str, evidence: str) -> MemoryNote:
    return _note(note_id, "构建用 uv", evidence=evidence)


def test_r8_evidence_breaks_a_tie_between_identical_notes():
    """Same text, same metadata: the only difference is where it came from."""
    notes = [_ev_note("a", "model"), _ev_note("b", "tool")]
    ranked = rank_notes(notes, "构建用什么工具", params=RecallParams(), now=NOW)
    assert [r.note.id for r in ranked] == ["b", "a"]


def test_r8_evidence_never_beats_a_higher_score():
    notes = [
        _note("strong", "构建用 uv，不要用 poetry", importance=5, evidence="model"),
        _note("weak", "构建用 uv", importance=1, evidence="tool"),
    ]
    ranked = rank_notes(notes, "构建用什么工具", params=RecallParams(), now=NOW)
    assert ranked[0].note.id == "strong"
    assert ranked[0].score > ranked[-1].score


def test_r8_blank_evidence_ranks_like_model_not_worse():
    """Rows from before R6d recorded evidence must not be punished."""
    notes = [_ev_note("legacy", ""), _ev_note("stated", "model"), _ev_note("done", "tool")]
    ranked = rank_notes(notes, "构建用什么工具", params=RecallParams(), now=NOW)
    assert ranked[0].note.id == "done"
    assert [r.note.id for r in ranked[1:]] == ["legacy", "stated"]


def test_r8_a_lower_score_is_never_reordered_by_evidence():
    strong = _note("strong", "构建用 uv，不要用 poetry", importance=5, evidence="model")
    weak = _note("weak", "构建用 uv", importance=1, evidence="tool")
    ranked = rank_notes([weak, strong], "构建用什么工具", params=RecallParams(), now=NOW)
    assert [r.note.id for r in ranked] == ["strong", "weak"]


# ---- R10b: bm25 ranking, a real floor, and the band ----


def test_r10b_bm25_prefers_the_document_that_answers_the_question():
    notes = [
        _note("loud", "docs 目录的章节要重写一遍", age_days=0, importance=5),
        _note("real", "构建流程：make build 会先跑 lint 再编译，产物在 dist/", age_days=1),
    ]
    scores = bm25_scores(notes, tokenize("make build 产物在哪"))
    assert scores["real"] > scores["loud"]


def test_r10b_bm25_gives_a_document_that_shares_nothing_a_zero():
    notes = [_note("a", "payment service timeout"), _note("b", "构建用 make")]
    scores = bm25_scores(notes, tokenize("完全不相干的查询"))
    assert scores == {"a": 0.0, "b": 0.0}


def test_r10b_the_absolute_floor_kills_a_fresh_important_irrelevant_note():
    """The measured hole (spec 1.1): 0.20*1.0 + 0.15*1.0 = 0.35 > threshold 0.25."""
    loud = _note("loud", "今天要把 docs 目录重写一遍", importance=5, age_days=0)
    real = _note("real", "上下文压缩有四层：L3 落盘、L1 裁剪、L2 信封、L4 摘要", age_days=30)
    params = RecallParams()
    assert params.min_relevance == 0.25
    kept = [hit.note.id for hit in rank_notes([loud, real], "上下文压缩分几层",
                                               params=params, now=NOW)]
    assert kept == ["real"]


def test_r10b_the_relative_band_drops_the_tail():
    notes = [
        _note("best", "上下文压缩有四层", age_days=5),
        _note("weak", "上下文里有压缩这个词，仅此而已", age_days=5),
    ]
    params = RecallParams(relative_band=0.99)
    result = rank_notes_detailed(notes, "上下文压缩分几层", params=params, now=NOW)
    assert [hit.note.id for hit in result.kept] == ["best"]
    assert [hit.note.id for hit in result.rejected] == ["weak"]


def test_r10b_matched_terms_lists_only_the_query_terms_that_hit():
    notes = [_note("a", "上下文压缩有四层", age_days=5)]
    result = rank_notes_detailed(notes, "上下文压缩有四层", now=NOW)
    assert result.kept[0].matched_terms
    assert set(result.kept[0].matched_terms) <= set(tokenize("上下文压缩有四层"))


def test_r10b_a_query_the_corpus_never_says_recalls_nothing_rather_than_the_freshest():
    """Spec 1.1: sorting by freshness alone answers a question nobody asked."""
    notes = [_note("fresh", "payment service timeout", age_days=0, importance=5)]
    result = rank_notes_detailed(notes, "Kubernetes 集群有多少节点", now=NOW)
    assert result.kept == ()
    assert result.no_vocabulary_match is True
    assert "kubernetes" in result.terms_unknown


def test_r10b_the_decoy_still_leaks_and_says_it_matched_one_word():
    """Spec 1.6: this is the hole we are NOT fixing. Pin it so nobody re-breaks it.

    What this pins is exactly two things: the decoy survives the gates, and it
    reports exactly one matched term. That is the whole of R10's obligation
    here. It is deliberately *not* pinned as a good hit — docs/r10-analysis.md
    §1.6 records that three candidate gates for it were measured and each one
    killed paraphrase recall, so the leak is accepted and made visible through
    matched_terms rather than gated away. The note wording is fixture data
    chosen so the decoy's coverage clears the floor; it is not the point.

    The third note is load-bearing and was not in the plan. With only the decoy
    and the honest note, `命令`/`子命` have df=1 against `cli`'s df=2, so idf
    splits 0.18 / 0.69 / 0.69 and the decoy's relevance is 0.116 — under the
    0.25 floor, which would gate the very leak this test exists to pin. Giving
    those terms a second holder restores the shape of the real corpus, where
    n52 leaks with matched_terms=('cli',), and the decoy lands at 0.33.
    """
    decoy = _note("decoy", "今天要把 docs 目录里的 CLI 章节重写一遍", age_days=0, importance=5)
    honest = _note("honest", "CLI 子命令要在 config 里注册", age_days=30)
    other = _note("other", "命令和子命在别处的说明文字", age_days=20)
    result = rank_notes_detailed([decoy, honest, other], "CLI 命令怎么加一个新子命令", now=NOW)
    leaked = [hit for hit in result.kept if hit.note.id == "decoy"]
    assert leaked, "spec 1.6 says the decoy is not gated away; if this passes, re-read it"
    assert len(leaked[0].matched_terms) == 1


def test_r10b_recency_source_accessed_is_off_by_default():
    touched = _note("a", "上下文压缩有四层", age_days=90).touch("2026-09-27T12:00:00Z")
    query = "上下文压缩有四层"
    created = rank_notes_detailed([touched], query, now=NOW).kept[0]
    accessed = rank_notes_detailed(
        [touched], query, params=RecallParams(recency_source="accessed"), now=NOW
    ).kept[0]
    assert created.score < accessed.score
