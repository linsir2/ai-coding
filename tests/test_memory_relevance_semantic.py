"""TDD (R10d-3): the ranker accepts an optional semantic signal.

The whole contract is one sentence: ``semantic=None`` behaves exactly as
today.  Everything else — the OR branch, the blend — only exists when a
mapping is actually passed.
"""

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
