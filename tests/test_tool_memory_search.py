"""TDD (R3b): the on-demand recall tool.

This is the cache-friendly recall channel (docs/r3-analysis.md 1.13): the model asks
for what it needs, the result is appended to the history as a normal tool message,
and every later turn keeps hitting the prompt cache on it.  The tool is read-only,
so the permission gate must allow it without a prompt.
"""

import json

import pytest

from ai_coding.domain.message import ChatMessage, ToolResult
from ai_coding.memory.note import MemoryNote, NoteKind
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.tools.memory_search_tool import MemorySearchTool


def _service(tmp_path, *texts: str) -> MemoryService:
    store = MemoryStore(tmp_path / "m.json")
    for text in texts:
        store.add_note(MemoryNote(id=f"id-{abs(hash(text)) % 10_000}", text=text))
    return MemoryService(store)


@pytest.mark.asyncio
async def test_search_returns_structured_notes(tmp_path):
    svc = _service(tmp_path, "项目用 uv 管理依赖")
    tool = MemorySearchTool(svc)

    result = await tool.execute({"query": "依赖怎么装"})

    assert result.success is True
    assert result.data is not None
    notes = result.data["notes"]
    assert len(notes) == 1
    assert notes[0]["text"] == "项目用 uv 管理依赖"
    assert notes[0]["kind"] == "fact"
    assert 0.0 < notes[0]["score"] <= 1.5
    assert "uv" in result.output  # the model reads this


@pytest.mark.asyncio
async def test_empty_query_is_a_structured_error(tmp_path):
    tool = MemorySearchTool(_service(tmp_path))
    result = await tool.execute({"query": "   "})
    assert result.success is False
    assert result.error == "empty_query"
    assert result.data == {"query": ""}


@pytest.mark.asyncio
async def test_no_hits_is_a_successful_empty_answer(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "payment service timeout"))
    result = await tool.execute({"query": "how do I add a dependency"})
    assert result.success is True
    # R10c: the payload deliberately grew a diagnosis; only `notes` is empty now.
    assert result.data["notes"] == []
    assert result.data["no_match"] is True


@pytest.mark.asyncio
async def test_kind_filter(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="项目用 uv 管理依赖", kind=NoteKind.FACT))
    store.add_note(
        MemoryNote(id="b", text="决定：依赖管理统一用 uv", kind=NoteKind.DECISION)
    )
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "uv 依赖", "kind": "decision"})

    assert [note["kind"] for note in result.data["notes"]] == ["decision"]


@pytest.mark.asyncio
async def test_tool_is_read_only_and_parallel_safe(tmp_path):
    tool = MemorySearchTool(_service(tmp_path))
    assert tool.name == "memory_search"
    assert tool.parallel_safe is True
    assert PermissionGate().check("memory_search", {}) == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_tool_survives_a_broken_memory(tmp_path):
    class _Broken:
        async def recall_notes(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    tool = MemorySearchTool(_Broken())
    result = await tool.execute({"query": "anything"})
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error == "memory_error"


@pytest.mark.asyncio
async def test_query_can_be_built_from_the_tool_context(tmp_path):
    """Without an explicit query the tool falls back to the parent turn's view."""
    svc = _service(tmp_path, "项目用 uv 管理依赖")
    tool = MemorySearchTool(svc)
    ctx = type(
        "C",
        (),
        {
            "messages": (ChatMessage(role="user", content="依赖怎么装", timestamp="t"),),
            "instructions": None,
        },
    )()

    result = await tool.execute({}, ctx)  # type: ignore[arg-type]

    assert result.success is True
    assert result.data["notes"]


# ---- R8b: filtering by where a note came from (D-4) ------------------------


# These tests assert *which ids come back per evidence filter*, not how they rank.
# The wording is chosen so every note clears the relevance floor: in a 3-note
# corpus idf is extreme, so a note that covers only the common bigram scores
# 0.221 and is gated out, which would fail these for the wrong reason.
def _evidence_service(tmp_path) -> MemoryService:
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="t1", text="构建用 uv", evidence="tool"))
    store.add_note(MemoryNote(id="m1", text="构建用 uv，改用别家", evidence="model"))
    store.add_note(MemoryNote(id="l1", text="构建用 uv，老数据", evidence=""))
    return MemoryService(store)


@pytest.mark.asyncio
async def test_r8_evidence_filter_keeps_only_tool_backed(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么", "evidence": "tool"})

    assert result.success is True
    assert result.data is not None
    assert [n["id"] for n in result.data["notes"]] == ["t1"]
    assert result.data["notes"][0]["evidence"] == "tool"


@pytest.mark.asyncio
async def test_r8_evidence_filter_model_means_not_tool(tmp_path):
    """Notes from before R6d have no evidence field; they are not 'tool'."""
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么", "evidence": "model"})

    assert {n["id"] for n in result.data["notes"]} == {"m1", "l1"}


@pytest.mark.asyncio
async def test_r8_evidence_empty_is_exactly_today(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么"})

    assert {n["id"] for n in result.data["notes"]} == {"t1", "m1", "l1"}


@pytest.mark.asyncio
async def test_r8_unknown_evidence_is_an_error_not_a_silent_pass(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))

    result = await tool.execute({"query": "构建用什么", "evidence": "maybe"})

    assert result.success is False
    assert result.error == "bad_evidence"
    assert result.data == {"evidence": "maybe"}


# ---- R10b: the touch runs before the filters, and that is a defect ----------


@pytest.mark.asyncio
async def test_r10b_known_the_filters_run_after_the_touch_so_filtered_out_notes_count(tmp_path):
    """R10 defect, scheduled for Task 5: filtering happens after the touch.

    memory_search recalls (touching every hit) and only then applies kind/evidence,
    so a note the model never sees still gains a hit — and consolidate promotes on
    hits >= 3. Task 5 moves filtering ahead of ranking; this test should be inverted
    or deleted then, not before.
    """
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="项目用 uv 管理依赖", kind=NoteKind.FACT))
    store.add_note(
        MemoryNote(id="b", text="决定：依赖管理统一用 uv", kind=NoteKind.DECISION)
    )
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "uv 依赖", "kind": "decision"})

    # The model only ever sees the decision ...
    assert [note["id"] for note in result.data["notes"]] == ["b"]
    # ... but the fact was recalled, so it was touched, so it counts as a use.
    assert store.get("a").hits == 1


# ---- R10c: a miss says what to try next -------------------------------------


@pytest.mark.asyncio
async def test_r10c_a_hit_reports_which_words_matched(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "项目用 uv 管理依赖"))

    result = await tool.execute({"query": "依赖怎么装"})

    assert result.data["no_match"] is False
    assert result.data["notes"][0]["matched_terms"] == ["依赖"]
    assert "依赖" in result.data["terms_known"]


@pytest.mark.asyncio
async def test_r10c_a_miss_names_the_words_memory_has_never_heard(tmp_path):
    """Measured: tokenize('依赖怎么装') -> ['依赖','赖怎','么装']; only 依赖 is in the corpus."""
    tool = MemorySearchTool(_service(tmp_path, "payment service timeout"))

    result = await tool.execute({"query": "依赖怎么装"})

    assert result.success is True
    assert result.data["notes"] == []
    assert result.data["no_match"] is True
    assert set(result.data["terms_unknown"]) == {"依赖", "赖怎", "么装"}
    assert result.data["terms_known"] == []


@pytest.mark.asyncio
async def test_r10c_near_misses_are_bounded_and_never_overlap_the_hits(tmp_path):
    """The corpus is not the brief's: 3 notes that clear the gate, 5 that do not.

    With the brief's six near-identical notes every note *passed* the gate, so
    ``rejected`` was empty, ``near_misses`` was ``[]`` and both assertions below
    held vacuously — a test that cannot fail lets the invariant rot.  These
    notes split the two sets, so ``len(misses) == 3`` (the bound, exactly) and
    the disjointness is a real claim about a real set.
    """
    store = MemoryStore(tmp_path / "m.json")
    strong = ["依赖管理用 uv", "依赖管理统一走 uv", "依赖管理方案是 uv"]
    weak = [
        "依赖已经装好了",
        "管理一个仓库的流程",
        "依赖锁文件怎么生成",
        "管理权限是谁的",
        "依赖版本要手动固定",
    ]
    for index, text in enumerate([*strong, *weak]):
        store.add_note(
            MemoryNote(id=f"n{index}", text=text,
                       created_time="2026-09-10T00:00:00+00:00")
        )
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "依赖管理"})

    hits = {note["id"] for note in result.data["notes"]}
    misses = {note["id"] for note in result.data["near_misses"]}
    assert len(misses) == 3
    assert not (hits & misses)


@pytest.mark.asyncio
async def test_r10c_the_index_map_is_bounded_and_only_on_a_miss(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    for index in range(500):
        store.add_note(
            MemoryNote(id=f"n{index:04d}", text=f"第 {index} 条无关笔记",
                       created_time="2026-09-01T00:00:00+00:00")
        )
    tool = MemorySearchTool(MemoryService(store))

    miss = await tool.execute({"query": "Kubernetes 集群有多少节点"})
    hit = await tool.execute({"query": "第 3 条无关笔记"})

    assert "index" in miss.data
    assert len(json.dumps(miss.data["index"], ensure_ascii=False)) <= 400
    assert miss.data["index"]["active"] == 500
    assert sum(miss.data["index"]["by_kind"].values()) == 500
    assert len(miss.data["index"]["recent"]) <= 3
    assert "index" not in hit.data


@pytest.mark.asyncio
async def test_r10c_the_index_count_is_active_not_total(tmp_path):
    """The key is "active" because the value is the active count, and because
    status() already spends "total" on all notes including superseded ones.

    A key-name assertion alone would not catch a revert that kept the name and
    changed the set being counted, which is the half of the bug that lies.
    """
    store = MemoryStore(tmp_path / "m.json")
    for index in range(3):
        store.add_note(
            MemoryNote(id=f"a{index}", text=f"依赖管理第 {index} 条",
                       created_time="2026-09-10T00:00:00+00:00")
        )
    store.supersede("a0", MemoryNote(id="new", text="依赖管理统一走 uv",
                                     created_time="2026-09-11T00:00:00+00:00"))
    service = MemoryService(store)
    tool = MemorySearchTool(service)

    miss = await tool.execute({"query": "Kubernetes 集群有多少节点"})

    assert miss.data["index"]["active"] == 3
    assert service.status()["total"] == 4  # the two keys are not the same count
    assert "total" not in miss.data["index"]


@pytest.mark.asyncio
async def test_r10c_the_miss_text_tells_the_model_what_to_do(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "项目用 uv 管理依赖"))

    result = await tool.execute({"query": "发票抬头是多少"})

    text = result.output
    assert "没有可靠匹配" in text
    assert "索引里" in text
    assert "发票" in text
    assert "换一个说法" in text


@pytest.mark.asyncio
async def test_r10c_near_misses_can_be_turned_off(tmp_path):
    """Not in the brief: the new flag is public surface, so it needs a pin.

    Nothing else in the suite observes ``include_near_misses=False``.
    """
    store = MemoryStore(tmp_path / "m.json")
    for index in range(6):
        store.add_note(
            MemoryNote(id=f"n{index}", text=f"依赖管理用 uv 第 {index} 条",
                       created_time=f"2026-09-{10 + index:02d}T00:00:00+00:00")
        )
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "依赖管理", "include_near_misses": False})

    assert result.data["notes"]
    assert "near_misses" not in result.data


@pytest.mark.asyncio
async def test_r10c_a_memory_without_diagnostics_still_works(tmp_path):
    """Duck-typed fakes predating recall_with_diagnostics keep working."""

    class _Legacy:
        async def recall_notes(self, *_args, **_kwargs):
            return []

    result = await MemorySearchTool(_Legacy()).execute({"query": "anything"})

    assert result.success is True
    assert result.data["notes"] == []
    assert result.data["no_match"] is True
