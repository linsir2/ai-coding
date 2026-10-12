"""TDD (R3a): MemoryService v2 — harvest with reconciliation, and consolidation.

Implemented cases (docs/r3-analysis.md section 8.4): the same fact harvested twice,
a contradiction superseding the old note, a near-duplicate merged by similarity,
delta-only extraction, "no extractor means no writing", ``enabled`` semantics, and
value-based consolidation.  The M3 API (``remember``/``recall``/``dream``) stays as
a deprecated surface and is covered at the bottom.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.memory.note import MemoryNote, NoteKind, new_note
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _cfg(**overrides):
    base = {
        "enabled": True,
        "auto_extract": True,
        "consolidate_threshold": 3,
        "consolidate_every_turns": 5,
        "max_index_entries": 100,
        "max_per_turn_injections": 3,
        "recall_threshold": 0.25,
        "recall_min_relevance": 0.05,
        "recency_half_life_days": 14.0,
        "index_max_chars": 600,
        "detail_max_chars": 1200,
        "harvest_max_messages": 12,
        "dedupe_similarity": 0.80,
        "pinned_kinds": ["decision"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp="t")


def _extractor_returning(*items):
    """An extractor that always answers with ``items`` (text or HarvestCandidate)."""

    seen: list[list] = []

    async def extract(messages, conclusion):
        seen.append(list(messages))
        return list(items)

    extract.seen = seen  # type: ignore[attr-defined]
    return extract


# ---- harvest ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_writes_only_non_empty_results(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, extractor=_extractor_returning(), config=_cfg())
    report = await svc.harvest([_user("闲聊")])
    assert store.count == 0
    assert report.changed is False


@pytest.mark.asyncio
async def test_harvest_is_idempotent_for_the_same_fact(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(
        store, extractor=_extractor_returning("项目用 uv 管理依赖"), config=_cfg()
    )
    first = await svc.harvest([_user("记住：项目用 uv 管理依赖")])
    second = await svc.harvest([_user("项目用 uv 管理依赖")])
    assert len(first.added) == 1
    assert store.count == 1  # case 3
    assert second.added == () and second.skipped  # already known -> NOOP


@pytest.mark.asyncio
async def test_harvest_supersedes_when_the_extractor_says_so(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    old_id = store.add_note(new_note("项目用 uv 管理依赖", kind=NoteKind.DECISION))
    extractor = _extractor_returning(
        HarvestCandidate(
            text="项目改用 poetry 管理依赖",
            kind=NoteKind.DECISION,
            action="supersede",
            target_id=old_id,
        )
    )
    svc = MemoryService(store, extractor=extractor, config=_cfg())

    report = await svc.harvest([_user("我们改成 poetry 了")])

    assert len(report.superseded) == 1
    old = store.get(old_id)
    assert old is not None and old.superseded_by is not None
    assert [n.text for n in store.active()] == ["项目改用 poetry 管理依赖"]  # case 4


@pytest.mark.asyncio
async def test_harvest_merges_a_near_duplicate_by_similarity(tmp_path):
    """Prototype G2: a re-worded fact must update, not duplicate."""
    store = MemoryStore(tmp_path / "m.json")
    keep_id = store.add_note(new_note("记住：项目用 uv 管理依赖"))
    svc = MemoryService(
        store,
        extractor=_extractor_returning("项目用 uv 管理依赖"),
        config=_cfg(),
    )
    report = await svc.harvest([_user("再次确认")])
    assert store.count == 1
    assert report.added == ()
    assert keep_id in report.updated or keep_id in report.skipped


@pytest.mark.asyncio
async def test_harvest_only_sees_the_messages_it_is_given(tmp_path):
    """Case 7: the loop hands over the delta, never the whole session."""
    extractor = _extractor_returning("x")
    svc = MemoryService(MemoryStore(tmp_path / "m.json"), extractor=extractor, config=_cfg())
    await svc.harvest([_user("第一轮"), _user("第二轮")])
    await svc.harvest([_user("第三轮"), _user("第四轮")])
    assert [len(batch) for batch in extractor.seen] == [2, 2]  # not [2, 4]


@pytest.mark.asyncio
async def test_harvest_caps_the_batch_size(tmp_path):
    extractor = _extractor_returning("x")
    svc = MemoryService(
        MemoryStore(tmp_path / "m.json"),
        extractor=extractor,
        config=_cfg(harvest_max_messages=2),
    )
    await svc.harvest([_user(str(i)) for i in range(6)])
    assert len(extractor.seen[0]) == 2


@pytest.mark.asyncio
async def test_harvest_without_an_extractor_writes_nothing(tmp_path):
    """Case 8: the offline fallback used to store raw chatter and 100 junk notes."""
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg())
    report = await svc.harvest([_user("先说一句预热的话"), _user("好的")])
    assert store.count == 0
    assert report.changed is False


@pytest.mark.asyncio
async def test_harvest_respects_the_switches(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    disabled = MemoryService(
        store, extractor=_extractor_returning("x"), config=_cfg(enabled=False)
    )
    no_auto = MemoryService(
        store, extractor=_extractor_returning("y"), config=_cfg(auto_extract=False)
    )
    assert (await disabled.harvest([_user("a")])).changed is False
    assert (await no_auto.harvest([_user("a")])).changed is False
    assert store.count == 0


@pytest.mark.asyncio
async def test_harvest_never_raises_when_the_extractor_fails(tmp_path):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("llm down")

    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, extractor=broken, config=_cfg())
    report = await svc.harvest([_user("a")])
    assert report.changed is False


@pytest.mark.asyncio
async def test_harvest_records_provenance_and_summary_input(tmp_path):
    extractor = _extractor_returning("项目用 uv 管理依赖")
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, extractor=extractor, config=_cfg())
    await svc.harvest(
        [_user("记住：项目用 uv 管理依赖")], session_id="s7", turn_index=3, summary="早期摘要"
    )
    note = store.all()[0]
    assert note.source_session == "s7"
    assert note.source_turn == 3
    assert extractor.seen[0][-1].role == "system"  # the L4 summary travels with the delta


@pytest.mark.asyncio
async def test_harvest_offers_the_known_memory_ids_to_the_extractor(tmp_path):
    """A contradiction can only be superseded if the extractor can name the target."""
    store = MemoryStore(tmp_path / "m.json")
    old_id = store.add_note(new_note("项目用 uv 管理依赖", kind=NoteKind.DECISION))
    extractor = _extractor_returning("nothing new")
    svc = MemoryService(store, extractor=extractor, config=_cfg())

    await svc.harvest([_user("我们把依赖管理从 uv 换成了 poetry")])

    hint = extractor.seen[0][-1].content or ""
    assert "<known-memory" in hint
    assert f"id={old_id}" in hint
    assert "项目用 uv 管理依赖" in hint


@pytest.mark.asyncio
async def test_harvest_enforces_the_index_cap(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(
        store,
        extractor=_extractor_returning(*[f"事实 {i}" for i in range(5)]),
        config=_cfg(max_index_entries=3),
    )
    await svc.harvest([_user("x")])
    assert store.count <= 3


# ---- recall ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_notes_ranks_and_explains(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("payment service timeout config", when=_stamp(30)))
    store.add_note(new_note("构建流程：make build 产物在 dist/", when=_stamp(1)))
    svc = MemoryService(store, config=_cfg())

    hits = await svc.recall_notes([_user("make build 产物在哪")], now=NOW)

    assert [h.note.text for h in hits] == ["构建流程：make build 产物在 dist/"]
    assert 0.0 < hits[0].score <= 1.5
    assert hits[0].relevance > 0


@pytest.mark.asyncio
async def test_recall_notes_returns_nothing_when_disabled(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("项目用 uv 管理依赖"))
    svc = MemoryService(store, config=_cfg(enabled=False))
    assert await svc.recall_notes([_user("uv 依赖")]) == []  # case 6


@pytest.mark.asyncio
async def test_recall_notes_applies_k(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    for index in range(5):
        store.add_note(new_note(f"项目 {index} 用 uv 管理依赖"))
    svc = MemoryService(store, config=_cfg(max_per_turn_injections=2))
    assert len(await svc.recall_notes([_user("uv 依赖")])) == 2


# ---- index block (the always-on constraint block) --------------------------


@pytest.mark.asyncio
async def test_index_block_contains_pinned_and_recent_decisions(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        new_note("安全红线：不得把密钥写进代码", kind=NoteKind.DECISION, pinned=True)
    )
    store.add_note(new_note("项目用 uv 管理依赖", kind=NoteKind.DECISION))
    store.add_note(new_note("某个普通事实"))
    svc = MemoryService(store, config=_cfg())

    block = svc.index_block()

    assert "安全红线" in block and "项目用 uv 管理依赖" in block
    assert "某个普通事实" not in block
    assert block.startswith("<memory-constraints>") and block.endswith("</memory-constraints>")
    assert "[decision#" in block


@pytest.mark.asyncio
async def test_index_block_is_empty_without_pinned_or_decisions(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("某个普通事实"))
    svc = MemoryService(store, config=_cfg())
    assert svc.index_block() == ""


@pytest.mark.asyncio
async def test_index_block_respects_the_character_budget(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    for index in range(10):
        store.add_note(
            new_note(f"决定 {index}：" + "很长的说明" * 12, kind=NoteKind.DECISION)
        )
    svc = MemoryService(store, config=_cfg(index_max_chars=120))
    block = svc.index_block()
    assert len(block) <= 160  # budget + the fixed wrapper


# ---- consolidation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidate_merges_near_duplicates(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    first = store.add_note(new_note("项目用 uv 管理依赖"))
    second = store.add_note(new_note("项目用 uv 来管理依赖"))
    svc = MemoryService(store, config=_cfg(consolidate_threshold=1))

    report = await svc.consolidate()

    assert store.count <= 2  # one of them is retired, both records stay auditable
    assert report.merged
    active_texts = [n.text for n in store.active()]
    assert first in {n.id for n in store.all()} or second in {n.id for n in store.all()}
    assert len(active_texts) == 1


@pytest.mark.asyncio
async def test_consolidate_promotes_often_used_notes(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    note_id = store.add_note(new_note("常用约定", importance=3))
    for _ in range(4):
        store.update(store.get(note_id).touch())  # type: ignore[union-attr]
    svc = MemoryService(store, config=_cfg(consolidate_threshold=1))

    report = await svc.consolidate()

    assert note_id in report.promoted
    assert store.get(note_id).importance == 4  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_consolidate_expires_stale_open_threads(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    old_id = store.add_note(
        new_note("待办：确认 CI 配置", kind=NoteKind.OPEN_THREAD, when=_stamp(90))
    )
    fresh_id = store.add_note(new_note("待办：写文档", kind=NoteKind.OPEN_THREAD))
    svc = MemoryService(store, config=_cfg(consolidate_threshold=1))

    report = await svc.consolidate(now=NOW)

    assert old_id in report.expired
    assert store.get(old_id) is None
    assert store.get(fresh_id) is not None


@pytest.mark.asyncio
async def test_consolidate_is_skipped_below_the_threshold(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("只有一条"))
    svc = MemoryService(store, config=_cfg(consolidate_threshold=10))
    report = await svc.consolidate()
    assert report == type(report)()  # empty report


@pytest.mark.asyncio
async def test_status_reports_counts(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("事实", kind=NoteKind.FACT))
    store.add_note(new_note("决定", kind=NoteKind.DECISION, pinned=True))
    svc = MemoryService(store, config=_cfg())
    status = svc.status()
    assert status["total"] == 2
    assert status["pinned"] == 1
    assert status["by_kind"]["decision"] == 1


# ---- deprecated M3 surface -------------------------------------------------


@pytest.mark.asyncio
async def test_deprecated_recall_returns_texts(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("user prefers pytest for tests"))
    store.add_note(new_note("deploy runs on linux"))
    svc = MemoryService(store, config=_cfg())
    assert await svc.recall([_user("how do I run my pytest tests?")], k=1) == [
        "user prefers pytest for tests"
    ]


@pytest.mark.asyncio
async def test_deprecated_remember_still_stores(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg())
    ids = await svc.remember([_user("remember python 3.10")], conclusion="py310")
    assert len(ids) == 1 and store.count == 1


@pytest.mark.asyncio
async def test_deprecated_dream_keeps_m3_semantics(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("one"))
    store.add_note(new_note("two"))
    svc = MemoryService(
        store,
        extractor=_extractor_returning("consolidated note"),
        config=_cfg(consolidate_threshold=1),
    )
    ids = await svc.dream([_user("context")])
    assert len(ids) == 1
    assert "consolidated note" in {n.text for n in store.all()}


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


# ---- R8b: the merge decision stays exactly as it is (D-13) ----------------


def test_r8_merging_ignores_evidence(tmp_path):
    """`_better_of` keeps deciding on (importance, hits, created_time)."""
    from ai_coding.memory.service import _better_of

    older_tool = MemoryNote(
        id="old",
        text="构建用 uv",
        evidence="tool",
        created_time="2026-01-01T00:00:00+00:00",
    )
    newer_stated = MemoryNote(
        id="new",
        text="构建用 uv",
        evidence="",
        created_time="2026-02-01T00:00:00+00:00",
    )
    keep, drop = _better_of(older_tool, newer_stated)
    assert (keep.id, drop.id) == ("new", "old")  # recency wins, not evidence

    hit_heavy = MemoryNote(
        id="hit",
        text="构建用 uv",
        hits=5,
        evidence="",
        created_time="2026-01-01T00:00:00+00:00",
    )
    keep, drop = _better_of(older_tool, hit_heavy)
    assert (keep.id, drop.id) == ("hit", "old")  # hits outrank evidence


@pytest.mark.asyncio
async def test_harvest_offers_the_scene_groups_the_memory_already_uses(tmp_path):
    """The cause of 36 labels over 43 notes: the extractor was shown no labels,
    so it invented one per note.  Showing it the head invites reuse — without
    forbidding a new label, because a label no memory shares cannot be a group
    and must not be blocked from existing (Heaps' law, arxiv.org/pdf/0704.3316).
    """
    store = MemoryStore(tmp_path / "m.json")
    for index in range(3):
        store.add_note(new_note(f"环境配置说明 {index}", scene="环境"))
    store.add_note(new_note("某个具体项目", scene="酒店管理系统"))
    extractor = _extractor_returning("nothing new")
    svc = MemoryService(store, extractor=extractor, config=_cfg())

    await svc.harvest([_user("新的依赖约定")])

    hint = extractor.seen[0][-1].content or ""
    assert "<scene-groups" in hint
    assert "环境" in hint
    assert "酒店管理系统" not in hint, "a one-note label is not worth offering"
    assert "新标签" in hint or "新的标签" in hint


@pytest.mark.asyncio
async def test_harvest_says_nothing_about_scenes_when_nothing_repeats(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(new_note("一条孤立的记忆", scene="甲"))
    extractor = _extractor_returning("nothing new")
    svc = MemoryService(store, extractor=extractor, config=_cfg())

    await svc.harvest([_user("新的依赖约定")])

    contents = " ".join(message.content or "" for message in extractor.seen[0])
    assert "<scene-groups" not in contents
