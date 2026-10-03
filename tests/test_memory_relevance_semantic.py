"""TDD (R10d-3): the ranker accepts an optional semantic signal.

The whole contract is one sentence: ``semantic=None`` behaves exactly as
today.  Everything else — the OR branch, the blend — only exists when a
mapping is actually passed.
"""

import pytest

from ai_coding.memory.note import MemoryNote
from ai_coding.memory.relevance import RecallParams, rank_notes, rank_notes_detailed

NOW_STR = "2026-09-01T00:00:00+00:00"


def _note(note_id: str, text: str, **over) -> MemoryNote:
    created = over.pop("created_time", NOW_STR)
    return MemoryNote(id=note_id, text=text, created_time=created, **over)


def test_r10d_none_means_today_byte_identical():
    notes = [
        _note("a", "项目依赖统一用 uv 管理"),
        _note("b", "构建流程用 make 跑 lint 再编译"),
        _note("c", "payment service timeout config"),
    ]
    params = RecallParams()

    without = [hit.note.id for hit in rank_notes(notes, "依赖怎么装", params=params)]
    with_none = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖怎么装", params=params, semantic=None).kept
    ]
    explicit = [hit.note.id for hit in rank_notes(notes, "依赖怎么装", params=params)]

    assert with_none == without == explicit


def test_r10d_the_or_branch_rescues_a_zero_overlap_note():
    """Coverage is 0.000 here; only cos >= min_cosine can keep it.

    Note ``b`` exists only so the query vocabulary is non-empty: with a single
    note the ranker returns before any gate, because nothing in the corpus uses
    any query word.  What matters is that ``a`` shares no word with the query.
    """
    notes = [
        _note("a", "项目依赖统一用 uv 管理", importance=3),
        _note("b", "发票很有用"),
    ]
    params = RecallParams(min_cosine=0.55)

    assert "a" not in [
        hit.note.id
        for hit in rank_notes_detailed(notes, "发票抬头是多少", params=params).kept
    ]
    rescued = rank_notes_detailed(
        notes, "发票抬头是多少", params=params, semantic={"a": 0.9}
    )
    assert "a" in [hit.note.id for hit in rescued.kept]


def test_r10d_below_the_cosine_floor_stays_out():
    notes = [_note("a", "项目依赖统一用 uv 管理", importance=3)]
    params = RecallParams(min_cosine=0.55)

    result = rank_notes_detailed(
        notes, "发票抬头是多少", params=params, semantic={"a": 0.4}
    )
    assert result.kept == ()


def test_r10d_fusion_orders_by_cosine_when_bm25_ties():
    """Same text, so BM25 is identical; the order must come from the blend."""
    notes = [_note("a", "项目依赖统一用 uv 管理"), _note("b", "项目依赖统一用 uv 管理")]
    params = RecallParams()

    result = rank_notes_detailed(
        notes, "依赖怎么装", params=params, semantic={"a": 0.2, "b": 0.9}
    )
    assert [hit.note.id for hit in result.kept] == ["b", "a"]


def test_r10d_single_candidate_normalises_to_one():
    """With nothing to compare against, one cosine is the best by definition.

    Fixture is chosen so the blend decides: importance=1 and old means
    recency+importance contribute ~0, so norm 1.0 gives 0.275 (kept) while
    norm 0.0 gives ~0.00 (dropped).  Deleting the hi==lo branch in _minmax
    fails this test.  Note ``b`` exists only so the query vocabulary is
    non-empty; ``a`` shares no word with the query.  The band is switched off
    because it is orthogonal to what is pinned here — ``b`` would otherwise
    set a floor that cuts ``a`` regardless of normalisation.
    """
    notes = [
        _note("a", "payment service timeout config", importance=1,
              created_time="2026-01-01T00:00:00+00:00"),
        _note("b", "发票很有用"),
    ]
    params = RecallParams(min_cosine=0.55, relative_band=0.0)

    result = rank_notes_detailed(
        notes, "发票抬头是多少", params=params, semantic={"a": 0.9}
    )
    assert "a" in [hit.note.id for hit in result.kept]


def test_r10d_the_gate_reads_raw_cosine_not_the_normalised_value():
    """Min-max maps the lower of two high cosines to 0.0.  The gate must not
    see that number, or the lower note would fail a floor it actually clears."""
    notes = [_note("a", "项目依赖统一用 uv 管理"), _note("b", "项目依赖统一用 uv 管理")]
    params = RecallParams(min_cosine=0.55)

    result = rank_notes_detailed(
        notes, "依赖怎么装", params=params, semantic={"a": 0.6, "b": 0.9}
    )
    assert {hit.note.id for hit in result.kept} == {"a", "b"}


# ---- R11d: the fusion rule is switchable, and RRF is not the default ------
#
# Measured before this was written (scripts/retrieval_cases.py, 124 queries,
# rank-only): blend reaches paraphrase R@3 0.41 / reachable R@3 1.00, RRF(K=60)
# reaches 0.26 / 0.96 — RRF trades away the set it was supposed to help. So it
# ships *available and off*, with the switch under test rather than the default.


def test_r11d_rrf_rank_term_is_the_documented_algebra():
    from ai_coding.memory.relevance import rrf_rank_term

    fused = rrf_rank_term({"a": 10.0, "b": 5.0}, {"a": 0.9, "b": 0.1})
    # a is first in both lists: 2/61, which normalises to exactly 1.0
    assert fused["a"] == pytest.approx(1.0)
    # b is second in both: 2/62, normalised by the same 2/(k+1) factor
    assert fused["b"] == pytest.approx((2 / 62) * (61 / 2))


def test_r11d_rrf_rank_term_counts_only_lists_that_contain_the_note():
    """A note one list never scored gets half credit, not a full first place."""
    from ai_coding.memory.relevance import rrf_rank_term

    fused = rrf_rank_term({"a": 10.0}, {"b": 0.9})

    assert fused == {"a": pytest.approx(0.5), "b": pytest.approx(0.5)}


def test_r11d_rrf_rank_term_is_empty_without_inputs():
    from ai_coding.memory.relevance import rrf_rank_term

    assert rrf_rank_term({}, {}) == {}
    assert rrf_rank_term({"a": 1.0}, {}) == pytest.approx({"a": 1.0})


def test_r11d_default_fusion_is_blend_and_blend_is_unchanged():
    """The default must be today's behaviour, byte for byte."""
    notes = [
        _note("a", "项目依赖统一用 uv 管理"),
        _note("b", "构建流程用 make 跑 lint 再编译"),
        _note("c", "payment service timeout config"),
    ]
    semantic = {"a": 0.9, "b": 0.2, "c": 0.4}

    assert RecallParams().recall_fusion == "blend"
    default = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖怎么装", params=RecallParams(),
                                       semantic=semantic).kept
    ]
    explicit = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖怎么装",
                                       params=RecallParams(recall_fusion="blend"),
                                       semantic=semantic).kept
    ]

    assert default == explicit


def test_r11d_an_unknown_fusion_value_is_treated_as_blend():
    """RecallParams is constructed directly by tests and scripts; it must not
    raise mid-recall over a string nobody validated."""
    notes = [_note("a", "项目依赖统一用 uv 管理"), _note("b", "构建流程用 make 编译")]
    semantic = {"a": 0.9, "b": 0.2}

    unknown = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖怎么装",
                                       params=RecallParams(recall_fusion="rrf-ish"),
                                       semantic=semantic).kept
    ]
    blend = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖怎么装", params=RecallParams(),
                                       semantic=semantic).kept
    ]

    assert unknown == blend


def test_r11d_rrf_without_a_semantic_signal_falls_back_to_blend():
    """One list is not a fusion. Without `semantic`, the lexical-only ranking
    must stay exactly as it is, or the switch would quietly penalise every
    user who never enabled the embedding tier."""
    notes = [
        _note("a", "项目依赖统一用 uv 管理"),
        _note("b", "构建流程用 make 跑 lint 再编译"),
    ]
    rrf = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖怎么装",
                                       params=RecallParams(recall_fusion="rrf"),
                                       semantic=None).kept
    ]
    blend = [
        hit.note.id
        for hit in rank_notes(notes, "依赖怎么装", params=RecallParams())
    ]

    assert rrf == blend


def test_r11d_rrf_and_blend_really_rank_differently():
    """A switch that changes nothing would pass every other test here."""
    notes = [
        _note("a", "依赖管理用 uv 安装"),
        _note("b", "依赖管理用 uv 安装和升级"),
        _note("c", "依赖管理用 uv 安装以及锁定版本"),
    ]
    # Lexically c is the best answer; semantically a is the closest paraphrase.
    semantic = {"a": 0.95, "b": 0.5, "c": 0.05}
    params = {"recall_fusion": "rrf"}

    rrf_order = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖管理用什么",
                                       params=RecallParams(**params),
                                       semantic=semantic).kept
    ]
    blend_order = [
        hit.note.id
        for hit in rank_notes_detailed(notes, "依赖管理用什么", params=RecallParams(),
                                       semantic=semantic).kept
    ]

    assert rrf_order != blend_order
    assert rrf_order[0] == "a"  # top of both lists in the RRF reading
