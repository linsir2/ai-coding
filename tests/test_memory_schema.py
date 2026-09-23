"""TDD (R3a): the structured harvest contract.

The M3 extractor returned one plain line per memory, which cannot express "this
replaces that note" (case 4 in docs/r3-analysis.md).  The new contract asks for
JSON objects with ``kind``/``importance``/``action``/``target_id``, but must stay
tolerant: a model that answers with a fenced code block, prose, or the old
line-per-note format must still be usable, and a broken answer must degrade to
nothing rather than raise (memory is best-effort by invariant).
"""

from ai_coding.memory.note import NoteKind
from ai_coding.memory.schema import HarvestCandidate, parse_candidates


def test_parses_a_plain_json_array():
    raw = (
        '[{"text": "项目用 uv 管理依赖", "kind": "decision", "importance": 5, '
        '"action": "add"}]'
    )
    candidates = parse_candidates(raw)
    assert len(candidates) == 1
    assert candidates[0].text == "项目用 uv 管理依赖"
    assert candidates[0].kind is NoteKind.DECISION
    assert candidates[0].importance == 5
    assert candidates[0].action == "add"


def test_parses_a_fenced_json_block_with_prose_around_it():
    raw = (
        "好的，我抽取到以下记忆：\n"
        "```json\n"
        '[{"text": "deploy 走 make build", "kind": "fact"}]\n'
        "```\n"
        "以上。"
    )
    candidates = parse_candidates(raw)
    assert [c.text for c in candidates] == ["deploy 走 make build"]


def test_supersede_carries_the_target_id():
    raw = (
        '[{"text": "项目改用 poetry 管理依赖", "kind": "decision", '
        '"action": "supersede", "target_id": "old123"}]'
    )
    candidate = parse_candidates(raw)[0]
    assert candidate.action == "supersede"
    assert candidate.target_id == "old123"


def test_unknown_action_falls_back_to_add():
    raw = '[{"text": "x", "action": "whatever"}]'
    assert parse_candidates(raw)[0].action == "add"


def test_importance_is_clamped_and_blank_rows_are_skipped():
    raw = '[{"text": "a", "importance": 99}, {"text": "   "}, {"no_text": 1}]'
    candidates = parse_candidates(raw)
    assert [c.text for c in candidates] == ["a"]
    assert candidates[0].importance == 5


def test_bad_rows_do_not_kill_good_ones():
    raw = '[{"text": "good"}, 42, {"text": "also good"}]'
    assert [c.text for c in parse_candidates(raw)] == ["good", "also good"]


def test_old_line_per_note_format_still_works():
    """Pre-R3 extractors answered with one memory per line."""
    raw = "项目用 uv 管理依赖\n不要用 pip\n\n"
    assert [c.text for c in parse_candidates(raw)] == [
        "项目用 uv 管理依赖",
        "不要用 pip",
    ]
    assert all(c.action == "add" for c in parse_candidates(raw))


def test_unusable_output_degrades_to_empty():
    assert parse_candidates("") == []
    assert parse_candidates("   \n  ") == []
    assert parse_candidates('[{"text": "unterminated"}') == []


def test_candidate_defaults():
    candidate = HarvestCandidate(text="项目用 uv")
    assert candidate.kind is NoteKind.FACT
    assert candidate.importance == 3
    assert candidate.action == "add"
    assert candidate.target_id is None
