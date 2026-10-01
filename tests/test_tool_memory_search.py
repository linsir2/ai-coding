"""TDD (R3b): the on-demand recall tool.

This is the cache-friendly recall channel (docs/r3-analysis.md 1.13): the model asks
for what it needs, the result is appended to the history as a normal tool message,
and every later turn keeps hitting the prompt cache on it.  The tool is read-only,
so the permission gate must allow it without a prompt.
"""

import json

import pytest

from ai_coding.domain.message import ChatMessage, ToolResult
from ai_coding.infra.time_utils import now_utc
from ai_coding.memory.note import MemoryNote, NoteKind
from ai_coding.memory.relevance import ScoredNote
from ai_coding.memory.service import MemoryService, RecallDiagnostics, _default_config
from ai_coding.memory.store import MemoryStore
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.tools.memory_search_tool import MemorySearchTool, _miss_text


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


# ---- R10b -> R10c: the filters run before the touch ---------------------------


@pytest.mark.asyncio
async def test_r10c_known_the_filters_now_run_before_the_touch(tmp_path):
    """Inverted from the R10b defect Task 5 fixed: a filtered-out note gains no hit.

    The R10b version asserted the opposite on purpose — `hits == 1` on a note the
    model never saw — to document a bug scheduled for this task.  The defect was
    not the count being wrong, it was the signal being wrong: `consolidate`
    promotes on `hits >= 3`, so three searches that all hid the same fact could
    promote it.
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
    # ... and the fact it never saw was never recalled, so it is not a use.
    assert store.get("a").hits == 0
    assert store.get("b").hits == 1


# ---- R10c: a miss says what to try next -------------------------------------


@pytest.mark.asyncio
async def test_r10c_a_hit_reports_which_words_matched(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "项目用 uv 管理依赖"))

    result = await tool.execute({"query": "依赖怎么装"})

    assert result.data["no_match"] is False
    assert result.data["notes"][0]["matched_terms"] == ["依赖"]
    assert "依赖" in result.data["terms_known"]


# A corpus where the "closest but unproven" set is dominated by a note that shares
# no word with the query: x0 is fresh, so its score comes from recency alone, and
# score — not relevance — is what orders `rejected`.  Measured, the buggy payload
# ships [x0 (relevance 0.0), m3, m1].
_FRESH, _STALE = "2026-09-30T00:00:00+00:00", "2026-06-01T00:00:00+00:00"


def _near_miss_service(tmp_path) -> MemoryService:
    store = MemoryStore(tmp_path / "m.json")
    for note_id, text, when in [
        ("n0", "依赖管理用 uv", _FRESH),
        ("n1", "依赖管理统一走 uv", _FRESH),
        ("n2", "依赖管理方案是 uv", _FRESH),
        ("x0", "今天天气不错适合出门散步", _FRESH),
        ("m1", "依赖锁文件怎么生成", _STALE),
        ("m2", "依赖版本要手动固定", _STALE),
        ("m3", "管理权限是谁的", _STALE),
    ]:
        store.add_note(MemoryNote(id=note_id, text=text, created_time=when))
    return MemoryService(store)


@pytest.mark.asyncio
async def test_r10c_a_miss_without_an_index_summary_does_not_invent_a_count(tmp_path):
    """Memory disabled is a documented config value, and the tool still registers.

    Three paths leave index_summary None — memory disabled, the ranker raising,
    and a pre-R10c double — and none of them means the index is empty.  The
    payload correctly omits `index`; the text must not print a count it never
    measured, because the text is what the model reads.
    """
    service = _service(tmp_path, "项目用 uv 管理依赖", "构建走 uv", "发票抬头是公司名")
    disabled = MemoryService(service.store, config=_default_config(enabled=False))
    tool = MemorySearchTool(disabled)

    result = await tool.execute({"query": "Kubernetes 集群有多少节点"})

    assert result.success is True
    assert "index" not in result.data  # the payload stays honest
    assert "没有可靠匹配" in result.output
    assert "0 条" not in result.output  # the text must not invent a size
    assert "换一个说法" in result.output  # ... but must still give a next step


@pytest.mark.asyncio
async def test_r10c_a_miss_caused_by_our_own_filter_says_so(tmp_path):
    """A miss the model caused itself must not be diagnosed as a vocabulary miss.

    Every query word was known and the ranking produced hits; `kind` threw them
    all away.  The generic advice ("rephrase, or narrow by kind") is
    self-contradictory here — the caller just narrowed by kind.  The note that
    would explain it was a kept hit, so it is in neither `notes` nor
    `near_misses` (which draws from `rejected`).
    """
    tool = MemorySearchTool(_near_miss_service(tmp_path))

    result = await tool.execute({"query": "依赖管理", "kind": "preference"})

    assert result.data["no_match"] is True
    assert result.data["notes"] == []
    assert result.data["terms_known"] and not result.data["terms_unknown"]
    assert "3 条" in result.output  # how many the filter removed
    assert "过滤" in result.output


@pytest.mark.asyncio
async def test_r10c_near_misses_do_not_ship_on_a_hit_unless_asked(tmp_path):
    """The common case must stay cheap: a hit carries notes, not a diagnosis."""
    tool = MemorySearchTool(_near_miss_service(tmp_path))

    by_default = await tool.execute({"query": "依赖管理"})
    opted_in = await tool.execute({"query": "依赖管理", "include_near_misses": True})

    assert by_default.data["notes"]  # it is a hit
    assert "near_misses" not in by_default.data
    assert opted_in.data["near_misses"]  # the model can still ask for candidates


@pytest.mark.asyncio
async def test_r10c_a_near_miss_that_matched_nothing_is_dropped(tmp_path):
    """A near miss that shares no word with the query is not "close"."""
    tool = MemorySearchTool(_near_miss_service(tmp_path))

    result = await tool.execute({"query": "依赖管理", "include_near_misses": True})

    near = result.data["near_misses"]
    assert "x0" not in {item["id"] for item in near}
    assert all(item["relevance"] > 0.0 for item in near)
    assert near  # and what survives is not simply an empty list


@pytest.mark.asyncio
async def test_r10c_the_miss_text_never_asserts_a_gate_it_could_not_see(tmp_path):
    """Unit test of the text builder: a measured index licenses a gate claim.

    End to end this branch is unreachable — a miss with every query term known
    always has near misses, or is filter-caused (which returns earlier) — so it is
    pinned here directly.  Without the `index is not None` precondition the text
    would tell a pre-R10c double, whose recall told us nothing at all, that its
    candidates missed a relevance gate it never ran.
    """
    measured = {"notes": [], "terms_known": ["依赖"], "terms_unknown": [],
                "index": {"active": 5, "by_kind": {"fact": 5}, "recent": []}}
    unmeasured = {k: v for k, v in measured.items() if k != "index"}

    assert "门槛" in _miss_text(measured)
    assert "门槛" not in _miss_text(unmeasured)
    assert "没有可靠匹配" in _miss_text(unmeasured)
    assert "换一个说法" in _miss_text(unmeasured)


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

    # Opt in: near misses no longer ship on a hit by default, and the
    # disjointness claim only means something when both sets are non-empty.
    result = await tool.execute({"query": "依赖管理", "include_near_misses": True})

    hits = {note["id"] for note in result.data["notes"]}
    misses = {note["id"] for note in result.data["near_misses"]}
    assert len(misses) == 3
    assert not (hits & misses)


@pytest.mark.asyncio
async def test_r10c_the_index_map_is_bounded_and_only_on_a_miss(tmp_path):
    """Note text is the spec's measured 40-88 chars, not 8.

    With 8-char notes the whole map serialises to 224 chars, so deleting
    _PREVIEW_CHARS outright leaves the assertion green and the bound is
    decorative.  At real note lengths removing the cap reaches past 400, which
    is what makes this a test of the cap rather than of the corpus.
    """
    store = MemoryStore(tmp_path / "m.json")
    filler = (
        "构建产物、依赖锁文件、缓存目录和测试快照都落在同一个工作目录下，"
        "需要定期清理归档，并且不能提交到仓库里，发布前还要确认一次"
    )
    for index in range(500):
        store.add_note(
            MemoryNote(id=f"n{index:04d}", text=f"第 {index} 条无关笔记：{filler}",
                       created_time="2026-09-01T00:00:00+00:00")
        )
    assert 67 <= len(f"第 0 条无关笔记：{filler}") <= 88  # uncapped must exceed 400
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
    # The no-filter advice line, pinned verbatim.  Every other assertion in this
    # file that touches it only requires 「换一个说法」, which a rewrite to a bare
    # 「换一个说法再搜。」 also satisfies — so the narrowing hint, the one thing that
    # tells the model what it may not have asked yet, was unpinned and could be
    # dropped without a single test noticing.
    assert "换一个说法再搜，或用 kind=decision/fact/preference/open_thread 缩小范围。" in text
    # The not-one-note diagnosis, not just the word list: no_vocabulary_match
    # says the content is simply not in memory, which is a different problem
    # from a partly-unknown wording and needs a different next move.
    assert "一条笔记都没出现过" in text


@pytest.mark.asyncio
async def test_r10c_near_misses_can_be_turned_off(tmp_path):
    """Not in the brief: the flag is public surface, so it needs a pin.

    Asked on a miss, where the default *does* ship candidates — otherwise this
    would pass even with the flag wired to nothing.
    """
    tool = MemorySearchTool(_near_miss_service(tmp_path))
    params = {"query": "依赖管理", "kind": "preference"}  # a miss with candidates

    assert (await tool.execute(params)).data["near_misses"]  # the default ships them
    off = await tool.execute({**params, "include_near_misses": False})

    assert off.data["no_match"] is True
    assert "near_misses" not in off.data


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


# ---- R10c: scope / since_days narrow the candidate set -----------------------


@pytest.mark.asyncio
async def test_r10c_the_tool_passes_scope_and_since_through(tmp_path):
    """Both notes answer the query, so each assertion is about the filter alone.

    The brief gave the two notes identical text; `add_note` dedupes on content, so
    only one survived and the test would have passed with the parameters unread.
    """
    store = MemoryStore(tmp_path / "m.json")
    stamp = now_utc()
    store.add_note(MemoryNote(id="a", text="依赖用 uv", scope="other",
                              created_time=stamp))
    store.add_note(MemoryNote(id="b", text="依赖用 poetry", scope="workspace",
                              created_time="2000-01-01T00:00:00+00:00"))
    tool = MemorySearchTool(MemoryService(store))

    scoped = await tool.execute({"query": "依赖", "scope": "other"})
    recent = await tool.execute({"query": "依赖", "since_days": 7})
    unfiltered = await tool.execute({"query": "依赖"})

    assert {note["id"] for note in unfiltered.data["notes"]} == {"a", "b"}
    assert [note["id"] for note in scoped.data["notes"]] == ["a"]
    assert [note["id"] for note in recent.data["notes"]] == ["a"]


@pytest.mark.asyncio
async def test_r10c_a_miss_names_the_filter_that_caused_it(tmp_path):
    """The count is measured, so the blame can be too.

    "kind/evidence" was true when those were the only two filters; naming them for
    a `scope` miss would send the model to loosen a filter it never set.
    """
    store = MemoryStore(tmp_path / "m.json")
    # Five notes clear the gate, three do not, and k=2.  All three numbers differ:
    # a truncated slice would say 2, the index line would say 8, and only the true
    # count is 5 — a bare "5 条" assertion would also match the index line.
    for index in range(5):
        store.add_note(MemoryNote(id=f"n{index}", text=f"依赖管理用 uv 第 {index} 条",
                                  scope="workspace",
                                  created_time="2026-09-30T00:00:00+00:00"))
    for index, text in enumerate(("依赖已经装好了", "管理一个仓库的流程",
                                  "依赖锁文件怎么生成")):
        store.add_note(MemoryNote(id=f"w{index}", text=text, scope="workspace",
                                  created_time="2026-06-01T00:00:00+00:00"))
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "依赖管理", "scope": "other", "k": 2})

    assert result.data["notes"] == []
    assert "有 5 条通过了相关度门槛" in result.output
    assert "有 2 条" not in result.output
    assert "scope" in result.output and "kind" not in result.output
    assert "放宽过滤条件" in result.output


@pytest.mark.asyncio
async def test_r10c_the_schema_documents_both_new_filters(tmp_path):
    """A parameter the schema does not mention is a parameter the model will not use."""
    tool = MemorySearchTool(_service(tmp_path))

    properties = tool.params_json_schema["properties"]

    assert properties["since_days"]["type"] == "integer"
    assert properties["scope"]["type"] == "string"
    assert "workspace" in properties["scope"]["description"]


# ---- R10c fix round 1: the diagnosis must be measured where it is claimed -----


@pytest.mark.asyncio
async def test_r10c_a_vocabulary_miss_behind_a_filter_still_names_the_words(tmp_path):
    """Critical: a filter that empties the candidate set must not fake a gate miss.

    `kind=preference` left nothing to rank, so the filtered ranking measured the
    query's vocabulary against an empty corpus and reported every word as known:
    「查询用词索引都认识」 next to an index that contains neither 量子 nor 纠缠.
    Reproduced before the fix, on a three-note corpus about 部署流水线.
    """
    store = MemoryStore(tmp_path / "m.json")
    for index in range(3):
        store.add_note(MemoryNote(id=f"n{index}", text=f"部署流水线第 {index} 条",
                                  kind=NoteKind.FACT,
                                  created_time="2026-09-30T00:00:00+00:00"))
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "量子纠缠", "kind": "preference"})

    # Measured: tokenize('量子纠缠') -> ['量子', '子纠', '纠缠']; none is in 部署流水线.
    assert set(result.data["terms_unknown"]) == {"量子", "子纠", "纠缠"}
    assert result.data["terms_known"] == []
    assert "一条笔记都没出现过" in result.output
    assert "查询用词索引都认识" not in result.output
    # The model just filtered by kind; telling it to narrow by kind is the same
    # self-contradiction the filtered-out branch was written to avoid.
    assert "缩小范围" not in result.output
    assert "放宽" in result.output


@pytest.mark.asyncio
async def test_r10c_the_tool_never_narrows_what_the_service_returned(tmp_path):
    """One filtering path, pinned by behaviour because grep cannot fail a build.

    A service that ignores `kind` has its hits passed straight through: the tool
    has no filter of its own left to hide them with. Re-adding `_filter_hits`
    makes this fail, which is the invariant the manual check used to cover.
    """

    class _IgnoresFilters:
        async def recall_with_diagnostics(self, _query, **_kwargs):
            hits = [
                ScoredNote(note=note, score=0.9, relevance=0.9, matched_terms=("依赖",))
                for note in (
                    MemoryNote(id="a", text="依赖用 uv", kind=NoteKind.FACT),
                    MemoryNote(id="b", text="依赖用 poetry", kind=NoteKind.DECISION),
                )
            ]
            return hits, RecallDiagnostics()

    result = await MemorySearchTool(_IgnoresFilters()).execute(
        {"query": "依赖", "kind": "fact"}
    )

    assert [note["id"] for note in result.data["notes"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_r10c_an_emptied_candidate_set_still_reports_a_measured_vocabulary(tmp_path):
    """N2: the probe was adopted only when it carried a *diagnosis*.

    An all-ASCII query whose every word is in the index, filtered down to nothing,
    gives a probe with `terms_known` set and both `terms_unknown` and
    `no_vocabulary_match` false — nothing for the old condition to fire on.  The
    filtered ranking was kept instead, and it was measured against an empty
    corpus, so the text said 「查询用词索引都认识」 with `terms_known == []`
    sitting in the payload beside it.
    """
    store = MemoryStore(tmp_path / "m.json")
    # One word per note: coverage is then 1/6 per note, under the 0.25 floor, so
    # the probe keeps nothing and neither does the filtered ranking — the two
    # differ only in what they can *say*, which is the whole point of the case.
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    for index, word in enumerate(words):
        store.add_note(MemoryNote(id=f"x{index}", text=f"note about {word} only",
                                  created_time="2026-09-28T00:00:00+00:00"))
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": " ".join(words), "kind": "preference"})

    assert result.data["no_match"] is True
    assert result.data["terms_known"] == words
    assert result.data["terms_unknown"] == []
    # And the payload and the text agree: the index does know every word.
    assert "查询用词索引都认识" not in result.output
    assert result.data["near_misses"]  # the probe's rejected set, which is the diagnosis
