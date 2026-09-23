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
from ai_coding.memory.relevance import RecallParams, coverage, rank_notes, recency
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
