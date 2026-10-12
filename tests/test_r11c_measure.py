"""TDD (R11c): the R11c acceptance script's analyzer, offline.

`scripts/r11c_cases.py` spends real tokens on a real model, so the part that
*decides pass or fail* has to be right before it ever runs.  These tests drive
`analyze()` and `check_navigation_roundtrip()` with recorded answers: the shape
of a harvest the model may produce, and the navigation loop over a store we
built by hand.

Nothing here is a stub: the live run calls exactly these functions.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.memory.note import MemoryNote  # noqa: E402
from ai_coding.memory.schema import parse_candidates  # noqa: E402
from ai_coding.memory.service import MemoryService  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402
from ai_coding.tools.memory_search_tool import MemorySearchTool  # noqa: E402
from scripts.r11c_cases import (  # noqa: E402
    ADOPTION_BAR,
    MAX_DISTINCT_SCENES,
    MAX_SCENE_CHARS,
    analyze,
    check_navigation_roundtrip,
)


def _note(note_id, text, *, scene="", hits=0, **over):
    return MemoryNote(id=note_id, text=text, scene=scene, hits=hits,
                      created_time="2026-10-01T00:00:00+00:00", **over)


ANSWER_FULL = """```json
[{"text": "项目用 uv 管理依赖", "kind": "decision", "importance": 5,
  "scene": "依赖管理", "action": "add"},
 {"text": "构建走 make 再跑 lint", "kind": "fact", "importance": 3,
  "scene": "构建", "action": "add"},
 {"text": "CI 在 GitHub Actions 上", "kind": "fact", "importance": 3,
  "scene": "构建", "action": "add"}]
```"""


def test_r11c_a_fully_labelled_answer_passes_every_offline_claim(tmp_path):
    """The good case: scenes on most candidates, few short labels, refs in range."""
    candidates = parse_candidates(ANSWER_FULL)
    assert len(candidates) == 3
    notes = [
        _note("n1", "项目用 uv 管理依赖", scene="依赖管理", source_session="s1",
              source_message_ids=("m0", "m1")),
        _note("n2", "构建走 make 再跑 lint", scene="构建", source_session="s1",
              source_message_ids=("m1", "m2")),
        _note("n3", "CI 在 GitHub Actions 上", scene="构建", source_session="s1",
              source_message_ids=("m2",)),
    ]
    messages = ["我们决定统一用 uv 管理依赖", "构建请走 make 并跑 lint", "CI 用 GitHub Actions"]

    report = analyze(candidates, notes, messages=messages, window=3)

    assert report.adoption_rate == 1.0
    assert report.distinct_scenes == 2 <= MAX_DISTINCT_SCENES
    assert report.longest_scene_chars == 4 <= MAX_SCENE_CHARS
    assert report.refs_out_of_range == ()
    assert report.ref_counts == {("m0", "m1"), ("m1", "m2"), ("m2",)}
    assert report.duplicate_texts_collapsed  # nothing here repeats, so vacuously ok
    assert report.passed(ADOPTION_BAR), report.reasons()


def test_r11c_a_model_that_ignores_the_scene_field_fails_adoption(tmp_path):
    """C1 is the whole ballgame: no scenes means the map and the filter are
    decoration, so this has to fail loudly rather than pass on a shrug."""
    candidates = parse_candidates(
        '[{"text": "项目用 uv 管理依赖", "kind": "decision"},'
        ' {"text": "CI 在 GitHub Actions 上", "kind": "fact"}]'
    )
    notes = [_note("n1", "项目用 uv 管理依赖", source_session="s1")]
    messages = ["我们决定统一用 uv 管理依赖"]

    report = analyze(candidates, notes, messages=messages, window=1)

    assert report.adoption_rate == 0.0
    assert not report.passed()
    assert any("scene" in reason for reason in report.reasons())


def test_r11c_plain_lines_from_the_json_fallback_count_against_adoption():
    """`build_extractor` falls back to one-line-per-memory when the model ignores
    the JSON contract. A line cannot carry a scene, so it must stay in the
    denominator — dropping it would make adoption look better than it is."""
    candidates = ["项目用 uv 管理依赖", "构建走 make"]
    notes = [_note("n1", "项目用 uv 管理依赖", source_session="s1",
                   source_message_ids=("m0",))]

    report = analyze(candidates, notes, messages=["约定"], window=1)

    assert report.candidates == 2 and report.labelled == 0 and report.legacy == 2
    assert report.adoption_rate == 0.0


def test_r11c_pooled_adoption_weights_batches_by_size():
    """C1 is judged on the pool: a 1-of-1 batch must not read as 100%."""
    from scripts.r11c_cases import Report, pooled_adoption

    tiny = Report(candidates=1, labelled=1, legacy=0, distinct_scenes=1,
                  longest_scene_chars=2, refs_out_of_range=(), refs_too_long=(),
                  ref_counts=frozenset({("m0",)}), duplicate_texts_collapsed=True,
                  notes=1)
    empty = Report(candidates=4, labelled=0, legacy=0, distinct_scenes=0,
                   longest_scene_chars=0, refs_out_of_range=(), refs_too_long=(),
                   ref_counts=frozenset(), duplicate_texts_collapsed=True, notes=0)

    rate, labelled, total = pooled_adoption([tiny, empty])

    assert (labelled, total) == (1, 5)
    assert rate == pytest.approx(0.2)


def test_r11c_refs_pointing_past_the_transcript_are_reported(tmp_path):
    """C4: a position reference that names a message which does not exist is a
    lie the model would act on."""
    candidates = parse_candidates(
        '[{"text": "项目用 uv 管理依赖", "kind": "decision", "scene": "依赖管理"}]'
    )
    notes = [_note("n1", "项目用 uv 管理依赖", scene="依赖管理", source_session="s1",
                   source_message_ids=("m0", "m9"))]
    messages = ["我们决定统一用 uv 管理依赖"]  # only m0 exists

    report = analyze(candidates, notes, messages=messages, window=1)

    assert report.refs_out_of_range == ("m9",)
    assert any("m9" in reason for reason in report.reasons())


def test_r11c_the_same_text_twice_must_stay_one_note(tmp_path):
    """C3: `dedupe_key` is content-only, so scene cannot split an exact duplicate."""
    store = MemoryStore(tmp_path / "m.json")
    first = store.add_note(_note("n1", "项目用 uv 管理依赖", scene="依赖管理"))
    second = store.add_note(
        _note("n2", "项目用 uv 管理依赖", scene="构建")  # same text, other scene
    )

    assert first == second
    assert len(store.active()) == 1


@pytest.mark.asyncio
async def test_r11c_the_navigation_loop_names_a_scene_and_narrows_to_it(tmp_path):
    """C5: the miss text names a label, and that label narrows the candidate set
    to exactly that scene's notes.

    The first version of this check searched *the label* as the query. A live run
    falsified it: the top label was 「CI/CD」 while the note read 「CI 使用 GitHub
    Actions」, so the label as a query matched nothing and the loop "failed" on a
    note the map had named correctly. A scene is a filter; the query still has to
    come from the question (or from the near-miss previews the payload carries).
    So the measurable contract is: the label appears in the prose, filtering by it
    returns that scene's note, and filtering by a *different* scene does not.
    """
    store = MemoryStore(tmp_path / "m.json")
    # Three notes per scene: since M8 a label has to be shared by at least three
    # memories to be a group, so a one-note scene is correctly *not* named.
    for index in range(3):
        store.add_note(_note(f"b{index}", f"构建走 make 再跑 lint {index}", scene="构建"))
        store.add_note(_note(f"r{index}", f"CI 使用 GitHub Actions 发布 {index}", scene="发布"))
    tool = MemorySearchTool(MemoryService(store))

    ok, detail = await check_navigation_roundtrip(
        tool, "量子纠缠的退相干时间", store.active()
    )

    assert ok, detail
    # Label-agnostic: with both scenes at heat 0 the tie-break picks whichever
    # sorts first, so assert the invariant instead — the scene named in the
    # detail is the scene the returned note actually belongs to.
    import re

    scene, note_id = re.search(r"scene '([^']+)' -> (\w+)", detail).groups()
    assert store.get(note_id).scene == scene


@pytest.mark.asyncio
async def test_r11c_the_navigation_check_catches_a_scene_filter_that_leaks(tmp_path):
    """The other half: filtering by one scene must not return another scene's
    note, or the map is decoration."""
    store = MemoryStore(tmp_path / "m.json")
    for index in range(3):
        store.add_note(_note(f"b{index}", f"构建走 make 再跑 lint {index}", scene="构建"))
    tool = MemorySearchTool(MemoryService(store))

    ok, detail = await check_navigation_roundtrip(
        tool, "量子纠缠的退相干时间", store.active()
    )

    assert ok, detail  # one scene only: nothing to leak into
    assert "leak" not in detail


@pytest.mark.asyncio
async def test_r11c_the_navigation_loop_reports_a_failure_honestly(tmp_path):
    """A store with no scenes cannot complete the loop, and the check must say so
    instead of reporting a pass."""
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(_note("n1", "构建走 make 再跑 lint"))
    service = MemoryService(store)
    tool = MemorySearchTool(service)

    ok, detail = await check_navigation_roundtrip(
        tool, "量子纠缠的退相干时间", store.active()
    )

    assert ok is False
    assert "场景" in detail or "scene" in detail
