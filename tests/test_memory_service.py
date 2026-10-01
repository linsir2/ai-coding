"""TDD (M3-3): MemoryService — recall / remember / dream with injected extractor.

Never raises; offline fallback extracts recent user/assistant signal when no
extractor is provided.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.infra.time_utils import now_utc
from ai_coding.memory.note import MemoryNote, NoteKind
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


def _cfg(**overrides):
    base = {
        "enabled": True,
        "auto_extract": True,
        "consolidate_threshold": 3,
        "max_index_entries": 100,
        "max_per_turn_injections": 4,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(content):
    return ChatMessage(role="user", content=content, timestamp="t")


@pytest.mark.asyncio
async def test_recall_offline_keyword(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add("user prefers pytest for tests")
    store.add("deploy runs on linux")
    svc = MemoryService(store, config=_cfg())
    history = [_user("how do I run my pytest tests?")]
    recalls = await svc.recall(history, k=1)
    assert recalls == ["user prefers pytest for tests"]


@pytest.mark.asyncio
async def test_remember_injects_and_stores(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg())
    ids = await svc.remember([_user("remember python 3.10")], conclusion="py310")
    assert len(ids) == 1
    assert store.count == 1


@pytest.mark.asyncio
async def test_remember_never_raises(tmp_path):
    store = MemoryStore(tmp_path / "m.json")

    async def bad_extractor(*_a, **_k):
        raise RuntimeError("llm down")

    svc = MemoryService(store, extractor=bad_extractor, config=_cfg())
    ids = await svc.remember([_user("hi")], "concl")  # must not raise
    assert ids == []


@pytest.mark.asyncio
async def test_dream_threshold(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    seen = []

    async def extractor(messages, conclusion):
        seen.append(list(messages))
        return ["consolidated note"]

    # count(0) < threshold(10) -> nothing happens
    below = MemoryService(store, extractor=extractor, config=_cfg(consolidate_threshold=10))
    assert await below.dream([_user("a")]) == []

    # raise the count above a low threshold -> consolidation runs
    store.add("one")
    store.add("two")
    svc2 = MemoryService(store, extractor=extractor, config=_cfg(consolidate_threshold=1))
    ids = await svc2.dream([_user("context")])
    assert len(ids) == 1  # one new entry stored
    assert "consolidated note" in {e.text for e in store.all()}


@pytest.mark.asyncio
async def test_extractor_injected_for_remember(tmp_path):
    store = MemoryStore(tmp_path / "m.json")

    async def extractor(messages, conclusion):
        return [f"summary:{conclusion}"]

    svc = MemoryService(store, extractor=extractor, config=_cfg())
    await svc.remember([_user("x")], "the deal")
    assert "summary:the deal" in {e.text for e in store.all()}


# ---- R10b: the usage signal is wired -----------------------------------------


def test_r10b_a_hit_counts_as_a_use(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", importance=4,
                   created_time="2026-09-20T00:00:00+00:00")
    )
    service = MemoryService(store)

    hits = asyncio.run(service.recall_notes("依赖怎么装", k=3))

    assert [hit.note.id for hit in hits] == ["a"]
    assert store.get("a").hits == 1
    assert store.get("a").last_used != ""


def test_r10b_a_miss_touches_nothing(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", importance=4,
                   created_time="2026-09-20T00:00:00+00:00")
    )
    service = MemoryService(store)

    assert asyncio.run(service.recall_notes("Kubernetes 集群有多少节点", k=3)) == []
    assert store.get("a").hits == 0


def test_r10b_consolidation_can_now_promote_from_real_use(tmp_path):
    """`hits >= 3 -> importance + 1` was unreachable while nothing touched on recall."""
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", importance=3,
                   created_time="2026-09-20T00:00:00+00:00")
    )
    service = MemoryService(store)
    for _ in range(3):
        asyncio.run(service.recall_notes("依赖怎么装", k=3))

    report = asyncio.run(service.consolidate(force=True))

    assert "a" in report.promoted
    assert store.get("a").importance == 4


# ---- R10c: filters narrow the candidate set BEFORE ranking --------------------


def test_r10c_kind_filter_does_not_shrink_the_result(tmp_path):
    """Regression: filtering after the top-k could return nothing at all.

    The corpus has to make the facts outrank the decision, or the test proves
    nothing: ``kind_boosts`` gives a decision +0.25, so the brief's corpus (four
    facts about 依赖管理 and one decision about 依赖管理) ranked the decision first
    and passed with the bug still in place.  Here the facts cover the whole query
    and the decision covers only "uv", so the old code kept three facts, spent the
    k on them and then filtered them all away — measured on HEAD:
    ``k=3 unfiltered -> [f0, f1, f2]`` / ``k=3, kind=decision -> []``.
    """
    store = MemoryStore(tmp_path / "m.json")
    for index, text in enumerate(("依赖管理用 uv 安装", "依赖管理用 uv 升级",
                                   "依赖管理用 uv 锁定", "依赖管理用 uv 发布")):
        store.add_note(
            MemoryNote(id=f"f{index}", text=text, kind=NoteKind.FACT,
                       created_time="2026-09-01T00:00:00+00:00")
        )
    store.add_note(
        MemoryNote(id="d1", text="决定：uv 作为默认工具",
                   kind=NoteKind.DECISION, created_time="2026-08-01T00:00:00+00:00")
    )
    service = MemoryService(store)

    unfiltered = asyncio.run(service.recall_with_diagnostics("依赖管理 uv", k=3))[0]
    assert [hit.note.id for hit in unfiltered] == ["f0", "f1", "f2"]  # the facts win

    hits = asyncio.run(service.recall_with_diagnostics("依赖管理 uv", k=3,
                                                       kinds=["decision"]))[0]
    narrow = asyncio.run(service.recall_with_diagnostics("依赖管理 uv", k=1,
                                                         kinds=["decision"]))[0]

    assert [hit.note.id for hit in hits] == ["d1"]
    assert [hit.note.id for hit in narrow] == ["d1"]


def test_r10c_scope_filters_candidates(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="依赖用 uv", scope="workspace",
                              created_time="2026-09-01T00:00:00+00:00"))
    store.add_note(MemoryNote(id="b", text="依赖用 poetry", scope="other",
                              created_time="2026-09-01T00:00:00+00:00"))
    service = MemoryService(store)

    hits = asyncio.run(service.recall_with_diagnostics("依赖", k=5, scope="other"))[0]

    assert [hit.note.id for hit in hits] == ["b"]


def test_r10c_since_days_filters_on_update_time(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    # Distinct texts on purpose: `add_note` dedupes on content, so the brief's two
    # identical notes were one note and this test could not have failed.
    store.add_note(MemoryNote(id="old", text="依赖管理用 uv 旧写法",
                              updated_time="2026-01-01T00:00:00+00:00",
                              created_time="2026-01-01T00:00:00+00:00"))
    store.add_note(MemoryNote(id="new", text="依赖管理用 uv 新写法",
                              updated_time="2026-09-27T00:00:00+00:00",
                              created_time="2026-09-27T00:00:00+00:00"))
    service = MemoryService(store)

    hits = asyncio.run(
        service.recall_with_diagnostics(
            "依赖", k=5, since_days=7, now=datetime(2026, 9, 28, tzinfo=timezone.utc)
        )
    )[0]

    assert [hit.note.id for hit in hits] == ["new"]


def test_r10c_since_days_reads_the_real_clock_when_now_is_not_given(tmp_path):
    """The production path passes no `now`, so the cutoff has to come from a clock.

    Pinned because the alternative is a hard-coded default: with one, `since_days`
    would compare every note against a constant date and silently return the whole
    index or none of it depending on when the code was written.
    """
    store = MemoryStore(tmp_path / "m.json")
    stamp = now_utc()
    store.add_note(MemoryNote(id="fresh", text="依赖用 uv",
                              updated_time=stamp, created_time=stamp))
    store.add_note(MemoryNote(id="stale", text="依赖用 uv",
                              updated_time="2000-01-01T00:00:00+00:00",
                              created_time="2000-01-01T00:00:00+00:00"))
    service = MemoryService(store)

    hits = asyncio.run(service.recall_with_diagnostics("依赖", k=5, since_days=1))[0]

    assert [hit.note.id for hit in hits] == ["fresh"]


def test_r10c_a_filter_caused_miss_reports_what_the_filter_removed(tmp_path):
    """`filtered_out` cannot be a subtraction any more: there is no post-filter list.

    Ranking now sees only the candidates the filters kept, so a miss measured on
    that set says nothing about what the filter threw away.  The count therefore
    comes from ranking the unfiltered index, which also carries the vocabulary
    diagnosis — the filtered run saw an empty set and would have claimed the
    index does not know the query at all.
    """
    store = MemoryStore(tmp_path / "m.json")
    for note_id in ("n0", "n1", "n2"):
        store.add_note(MemoryNote(id=note_id, text=f"依赖管理用 uv {note_id}",
                                  kind=NoteKind.FACT,
                                  created_time="2026-09-30T00:00:00+00:00"))
    service = MemoryService(store)

    hits, diagnostics = asyncio.run(
        service.recall_with_diagnostics("依赖管理", k=3, kinds=["preference"])
    )

    assert hits == []
    assert diagnostics.filtered_out == 3
    assert diagnostics.terms_known and not diagnostics.terms_unknown
    assert diagnostics.no_vocabulary_match is False
