"""TDD (R10a): the yardstick — corpus loading plus recall@k / MRR / FP@k maths.

These are the numbers every later scoring change has to be argued against, so
they are tested as arithmetic rather than trusted: a silently wrong recall
would make the rest of the round unfalsifiable.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_coding.memory.benchmark import EvalCase, evaluate, format_metrics, load_corpus

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _corpus(tmp_path, notes, queries) -> Path:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps({"notes": notes, "queries": queries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_r10a_load_corpus_builds_notes_and_cases(tmp_path):
    path = _corpus(
        tmp_path,
        [{"id": "n1", "kind": "decision", "importance": 4, "age_days": 7, "text": "用 uv"}],
        [{"category": "lexical", "query": "依赖", "relevant": ["n1"]}],
    )

    notes, cases = load_corpus(path, now=NOW)

    assert [n.id for n in notes] == ["n1"]
    assert notes[0].kind.value == "decision"
    assert notes[0].importance == 4
    assert notes[0].created_time == "2026-09-21T00:00:00+00:00"
    assert cases == [EvalCase("lexical", "依赖", frozenset({"n1"}))]


def test_r10a_recall_mrr_and_false_positive_share_are_what_they_claim():
    cases = [
        EvalCase("lexical", "q1", frozenset({"a"})),
        EvalCase("lexical", "q2", frozenset({"b"})),
        EvalCase("lexical", "q3", frozenset({"c"})),
    ]
    ranking = {"q1": ["a", "x", "y"], "q2": ["x", "b"], "q3": ["x", "y", "z"]}

    metrics = evaluate(lambda query: ranking[query], cases)

    assert metrics.answerable == 3
    assert metrics.recall_at(1) == 1 / 3
    assert metrics.recall_at(3) == 2 / 3
    assert metrics.mrr == (1 + 0.5) / 3
    # per query: 2 of 3 junk, 1 of 2 junk, 3 of 3 junk -> averaged
    assert metrics.false_positive_share == pytest.approx((2 / 3 + 1 / 2 + 3 / 3) / 3)
    assert metrics.missed == ("q3",)


def test_r10a_a_no_answer_case_is_ok_only_when_nothing_came_back():
    cases = [
        EvalCase("noanswer", "q1", frozenset()),
        EvalCase("trap", "q2", frozenset()),
    ]
    metrics = evaluate(lambda query: [] if query == "q1" else ["z"], cases)

    assert metrics.answerable == 0
    assert metrics.no_answer == 2
    assert metrics.no_answer_ok == 1
    assert metrics.spurious == ("q2",)


def test_r10a_a_ranker_that_raises_counts_as_returning_nothing():
    def rank(query: str):
        raise RuntimeError("boom")

    metrics = evaluate(rank, [EvalCase("lexical", "q", frozenset({"a"}))])

    assert metrics.recall_at(1) == 0.0
    assert metrics.missed == ("q",)


def test_r10a_format_metrics_shows_the_numbers_it_computed():
    cases = [EvalCase("lexical", "q", frozenset({"a"}))]
    line = format_metrics("current", evaluate(lambda _q: ["a"], cases))

    assert line.startswith("current")
    assert "R@1=1.00" in line
    assert "FP@5=0.00" in line


def test_r10a_the_shipped_corpus_is_well_formed():
    root = Path(__file__).resolve().parent.parent
    notes, cases = load_corpus(root / "scripts" / "retrieval_corpus.json", now=NOW)

    ids = {note.id for note in notes}
    assert len(ids) == 52, f"note ids must be unique, got {len(ids)}"
    assert len(cases) == 46
    assert {case.category for case in cases} == {
        "lexical", "identifier", "paraphrase", "multi", "noanswer", "trap",
    }
    for case in cases:
        assert case.relevant <= ids, f"{case.query!r} points at an unknown note id"
        assert case.query.strip(), "a labelled question must not be blank"
    # Six questions have no answer on purpose: five noanswer plus one trap.
    unanswerable = [case for case in cases if not case.relevant]
    assert len(unanswerable) == 6
    assert any(case.category == "trap" for case in unanswerable)
