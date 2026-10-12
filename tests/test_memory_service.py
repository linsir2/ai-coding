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
from ai_coding.memory.relevance import rank_notes_detailed
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.service import (
    HarvestReport,
    MemoryService,
    RecallDiagnostics,
    _default_config,
    is_extraction_allowed,
)
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

    assert [hit.note.id for hit in hits] == ["d1"]


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
    # Five notes clear the gate and k=2, so a truncated slice would report 2 — the
    # count is a set size, and the corpus has to be able to tell the difference.
    for index in range(5):
        store.add_note(MemoryNote(id=f"n{index}", text=f"依赖管理用 uv 第 {index} 条",
                                  kind=NoteKind.FACT,
                                  created_time="2026-09-30T00:00:00+00:00"))
    service = MemoryService(store)

    hits, diagnostics = asyncio.run(
        service.recall_with_diagnostics("依赖管理", k=2, kinds=["preference"])
    )

    assert hits == []
    assert diagnostics.filtered_out == 5
    assert diagnostics.terms_known and not diagnostics.terms_unknown
    assert diagnostics.no_vocabulary_match is False


def test_r10c_since_days_of_zero_or_less_means_no_window(tmp_path):
    """`0` and a negative are not windows, and returning the whole index for `0`
    is the tool boundary's job to prevent — this is the service-side backstop, so
    a direct caller gets the same answer the tool would have given it."""
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="old", text="依赖管理用 uv 旧写法",
                              updated_time="2000-01-01T00:00:00+00:00",
                              created_time="2000-01-01T00:00:00+00:00"))
    service = MemoryService(store)

    for since_days in (0, -5):
        hits = asyncio.run(service.recall_with_diagnostics("依赖", k=5,
                                                           since_days=since_days))[0]
        assert [hit.note.id for hit in hits] == ["old"], since_days


def test_r10c_since_days_of_an_empty_string_is_not_a_silent_empty_recall(tmp_path):
    """N3: `float("")` raised inside `_candidates`, and nothing reported it.

    `since_days` is `int | None` by annotation, but the caller is duck-typed and
    the tool boundary is a string-to-int parse of whatever the model sent — an
    unfilled field arrives as `""`.  The `except Exception` around the whole
    ranking is what memory's "never raises" invariant needs, and it turns this
    into an empty recall with no diagnosis at all: `index_summary is None`, so
    the miss text cannot even count the index.
    """
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="依赖管理统一用 uv",
                              created_time="2000-01-01T00:00:00+00:00",
                              updated_time="2000-01-01T00:00:00+00:00"))
    service = MemoryService(store)

    hits, diagnostics = asyncio.run(
        service.recall_with_diagnostics("依赖管理", k=5, since_days="")
    )

    # "" is not a window, so this is the whole index — the same answer `0` and
    # `-5` get.  The note is from 2000 on purpose: `since_days=""` must not
    # become a window that excludes everything, which is what a NaN-ish
    # interpretation would do.
    assert [hit.note.id for hit in hits] == ["a"]
    assert diagnostics.index_summary is not None


def test_r10_only_the_notes_the_model_saw_count_as_a_use(tmp_path):
    """A use is something the model was shown, not something that ranked.

    `docs/data-contracts.md` and `docs/features.md` both state this invariant
    ("被过滤掉的候选不算使用（模型没看见它）"), and nothing pinned it: replacing
    `touch_notes([hit.note.id for hit in hits])` with a touch of every candidate
    left the whole suite green.  The corpus matters — with one matching note the
    two behaviours are indistinguishable.

    Six notes here all cover 「依赖管理 uv」 completely, so all six clear the
    relevance floor, the threshold and the band (measured: `limit=None` keeps all
    six). `k=1` returns exactly one of them. Touching every candidate would
    record six uses where the model saw one, which is not a rounding error: it
    feeds `hits >= 3 -> importance + 1` and the eviction value function
    `importance + 0.5*min(hits,4) + 0.5*freshness` with signal the model never
    produced. Two recalls of the same query would then promote a note on the
    strength of a `k` the caller chose, not of anything recalled.
    """
    store = MemoryStore(tmp_path / "m.json")
    texts = ("依赖管理用 uv 安装", "依赖管理用 uv 升级", "依赖管理用 uv 锁定",
             "依赖管理用 uv 发布", "依赖管理用 uv 测试", "依赖管理用 uv 部署")
    for index, text in enumerate(texts):
        store.add_note(MemoryNote(id=f"f{index}", text=text, kind=NoteKind.FACT,
                                  created_time="2026-09-25T00:00:00+00:00"))
    service = MemoryService(store)

    # The premise: all six really do clear the gate, so "only one was touched"
    # is a decision about k and not about the gates rejecting the rest.
    unranked = rank_notes_detailed(store.active(), "依赖管理 uv", limit=len(texts))
    assert [hit.note.id for hit in unranked.kept] == [f"f{i}" for i in range(6)]

    hits, _diagnostics = asyncio.run(
        service.recall_with_diagnostics("依赖管理 uv", k=1)
    )

    assert [hit.note.id for hit in hits] == ["f0"]
    counts = {note.id: note.hits for note in store.active()}
    assert counts == {"f0": 1, "f1": 0, "f2": 0, "f3": 0, "f4": 0, "f5": 0}


def test_r11a_extraction_gate_missing_config_allows_all():
    """R11a: no config section means the historical always-on behaviour."""
    assert is_extraction_allowed(SimpleNamespace(), "harvest") is True
    assert is_extraction_allowed(SimpleNamespace(), "memory_write") is True
    assert is_extraction_allowed(None, "harvest") is True


def test_r11a_extraction_gate_disabled_denies_everything():
    """R11a: `extraction_enabled=False` is the master kill switch."""
    cfg = SimpleNamespace(extraction_enabled=False, extraction_extractors=["harvest"])
    assert is_extraction_allowed(cfg, "harvest") is False
    assert is_extraction_allowed(cfg, "memory_write") is False


def test_r11a_extraction_gate_allowlist_filters_assets():
    """R11a: only listed assets run; a malformed list never silently stops writes."""
    cfg = SimpleNamespace(extraction_enabled=True, extraction_extractors=["memory_write"])
    assert is_extraction_allowed(cfg, "harvest") is False
    assert is_extraction_allowed(cfg, "memory_write") is True
    assert is_extraction_allowed(SimpleNamespace(extraction_enabled=True), "harvest") is True
    malformed = SimpleNamespace(extraction_enabled=True, extraction_extractors="harvest")
    assert is_extraction_allowed(malformed, "harvest") is True


@pytest.mark.asyncio
async def test_r11a_harvest_respects_gate_leaves_index_untouched(tmp_path):
    """R11a: a gated harvest writes nothing — not even an eviction."""
    store = MemoryStore(tmp_path / "m.json")

    async def extractor(messages, summary):
        return ["门应该挡住这条"]

    cfg = _cfg(extraction_enabled=True, extraction_extractors=["memory_write"])
    svc = MemoryService(store, extractor=extractor, config=cfg)
    report = await svc.harvest([_user("hello")], turn_index=1, session_id="s")
    assert report == HarvestReport()
    assert store.count == 0

    cfg2 = _cfg(extraction_enabled=True, extraction_extractors=["harvest"])
    svc2 = MemoryService(store, extractor=extractor, config=cfg2)
    report2 = await svc2.harvest([_user("hello")], turn_index=1, session_id="s")
    assert len(report2.added) == 1
    assert store.count == 1


def test_r11a_write_note_respects_gate(tmp_path):
    """R11a: the model's own write channel has its own gate entry."""
    store = MemoryStore(tmp_path / "m.json")
    blocked = MemoryService(
        store, config=_cfg(extraction_enabled=True, extraction_extractors=["harvest"])
    )
    report = blocked.write_note("模型自写", kind="fact")
    assert report == HarvestReport()
    assert store.count == 0

    allowed = MemoryService(
        store, config=_cfg(extraction_enabled=True, extraction_extractors=["memory_write"])
    )
    report2 = allowed.write_note("模型自写", kind="fact")
    assert len(report2.added) == 1
    assert store.count == 1


@pytest.mark.asyncio
async def test_r11a_recall_ignores_tool_envelope(tmp_path):
    """R11a: an envelope pasted into the query must not change the ranking."""
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg())
    svc.write_note("构建用 uv 管理依赖", kind="fact")
    clean_hits, _diag = await svc.recall_with_diagnostics("构建用什么依赖", k=3)
    assert clean_hits  # premise: the clean query really hits
    noisy = "构建用什么依赖\n<tool_output tool=\"read\">\n1  hello world\n</tool_output>"
    noisy_hits, noisy_diag = await svc.recall_with_diagnostics(noisy, k=3)
    assert [hit.note.id for hit in noisy_hits] == [hit.note.id for hit in clean_hits]
    assert noisy_diag.terms_unknown == _diag.terms_unknown


def test_r11b_diagnostics_timed_out_defaults_false():
    """R11b: the two new diagnostic fields default to 'never happened'."""
    diagnostics = RecallDiagnostics()
    assert diagnostics.timed_out is False
    assert diagnostics.elapsed_seconds == 0.0


@pytest.mark.asyncio
async def test_r11b_recall_timeout_zero_disables(tmp_path):
    """R11b: `recall_timeout_seconds=0` means no budget, never a timeout."""
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg(recall_timeout_seconds=0))
    svc.write_note("构建用 uv 管理依赖", kind="fact")
    hits, diagnostics = await svc.recall_with_diagnostics("构建用什么依赖", k=3)
    assert hits
    assert diagnostics.timed_out is False


@pytest.mark.asyncio
async def test_r11b_recall_marks_timed_out_and_skips_touch(tmp_path):
    """R11b: over budget the hits still come back, but the use is not recorded.

    The timeout is a post-hoc guard, not a preemption: a synchronous BM25 loop
    has no await point to abandon, so what the budget buys is skipping the one
    optional side effect (the whole-file `touch_notes` write).
    """
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg(recall_timeout_seconds=1e-9))
    svc.write_note("构建用 uv 管理依赖", kind="fact")
    hits, diagnostics = await svc.recall_with_diagnostics("构建用什么依赖", k=3)
    assert hits  # a timeout must never starve the model
    assert diagnostics.timed_out is True
    assert diagnostics.elapsed_seconds >= 0.0
    assert [note.hits for note in store.active()] == [0]


@pytest.mark.asyncio
async def test_r11b_timed_out_probe_still_reports_filters(tmp_path):
    """R11b: the filter diagnosis is computed before the budget check matters."""
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg(recall_timeout_seconds=1e-9))
    svc.write_note("构建用 uv 管理依赖", kind="fact")
    hits, diagnostics = await svc.recall_with_diagnostics(
        "构建用什么依赖", k=3, kinds=["decision"]
    )
    assert hits == []
    assert diagnostics.timed_out is True
    assert diagnostics.filtered_out == 1


# ---- R11c: source_message_ids — the window the extractor was shown -------


@pytest.mark.asyncio
async def test_r11c_harvest_records_refs_for_the_window_it_showed_the_extractor(tmp_path):
    """Refs point at the messages the extractor read, not at the whole delta.

    The delta starts at absolute index 5 and holds 20 messages, but only the
    last `harvest_max_messages` (12) are handed to the extractor.  A ref for
    every delta message would attribute a note to 8 messages it never saw — the
    exact lie this field must not tell.
    """
    store = MemoryStore(tmp_path / "m.json")

    async def extractor(messages, summary):
        return ["构建用 uv 管理依赖"]

    svc = MemoryService(
        store,
        extractor=extractor,
        config=_cfg(harvest_max_messages=12, consolidate_threshold=1000),
    )
    delta = [_user(f"m{5 + i}") for i in range(20)]

    report = await svc.harvest(delta, turn_index=1, session_id="s1", source_offset=5)

    assert len(report.added) == 1
    refs = store.get(report.added[0]).source_message_ids
    assert refs == tuple(f"m{13 + i}" for i in range(12))


@pytest.mark.asyncio
async def test_r11c_harvest_without_an_offset_numbers_from_zero(tmp_path):
    store = MemoryStore(tmp_path / "m.json")

    async def extractor(messages, summary):
        return ["短对话里的一条记忆"]

    svc = MemoryService(store, extractor=extractor, config=_cfg(harvest_max_messages=12))
    delta = [_user("a"), _user("b")]

    report = await svc.harvest(delta, turn_index=1, session_id="s1")

    assert store.get(report.added[0]).source_message_ids == ("m0", "m1")


def test_r11c_write_note_leaves_refs_empty(tmp_path):
    """A note the model writes now has no position in a transcript to point at."""
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=_cfg())

    report = svc.write_note("模型当场写的一条", kind="decision")

    assert store.get(report.added[0]).source_message_ids == ()


@pytest.mark.asyncio
async def test_r11c_merge_keeps_the_winner_refs(tmp_path):
    """A near-duplicate merge keeps the surviving note's own provenance."""
    store = MemoryStore(tmp_path / "m.json")

    async def extractor(messages, summary):
        return ["项目用 uv 管理依赖"]

    svc = MemoryService(
        store, extractor=extractor, config=_cfg(harvest_max_messages=12,
                                                 consolidate_threshold=1000)
    )
    first = await svc.harvest([_user("a")], turn_index=1, session_id="s1", source_offset=4)
    keeper = store.get(first.added[0])
    assert keeper.source_message_ids == ("m4",)

    # Same kind, same text, no dedupe escape: `_reconcile` merges it away.
    await svc.harvest([_user("a")], turn_index=2, session_id="s1", source_offset=9)

    assert [n.source_message_ids for n in store.active()] == [("m4",)]


# ---- R11c: scene groups merging, filtering and the index map -------------


@pytest.mark.asyncio
async def test_r11c_reconcile_merges_only_within_the_same_scene(tmp_path):
    """Same kind, near-identical text, different scenes: two notes, not one."""
    store = MemoryStore(tmp_path / "m.json")
    long_text = "项目用 uv 管理依赖"
    near = "项目用 uv 管理依赖，不要用 pip"

    async def extractor(messages, summary):
        return [HarvestCandidate(text=long_text, kind=NoteKind.FACT, scene="构建")]

    svc = MemoryService(
        store,
        extractor=extractor,
        config=_cfg(dedupe_similarity=0.6, consolidate_threshold=1000),
    )
    first = await svc.harvest([_user("x")], turn_index=1, session_id="s1")

    async def other_extractor(messages, summary):
        return [HarvestCandidate(text=near, kind=NoteKind.FACT, scene="发布")]

    svc2 = MemoryService(
        store,
        extractor=other_extractor,
        config=_cfg(dedupe_similarity=0.6, consolidate_threshold=1000),
    )
    second = await svc2.harvest([_user("x")], turn_index=2, session_id="s1")

    assert first.added and second.added
    assert len(store.active()) == 2
    assert {n.scene for n in store.active()} == {"构建", "发布"}


@pytest.mark.asyncio
async def test_r11c_dedupe_hit_backfills_a_missing_scene(tmp_path):
    """The common order is harvest first, label later — so a later scene sticks.

    Without this, `memory_write(scene=...)` on a note the extractor had already
    written would be silently dropped by the dedupe branch and the scene map
    would stay empty forever.
    """
    store = MemoryStore(tmp_path / "m.json")

    async def plain(messages, summary):
        return [HarvestCandidate(text="项目用 uv 管理依赖", kind=NoteKind.FACT)]

    svc = MemoryService(store, extractor=plain, config=_cfg(consolidate_threshold=1000))
    first = await svc.harvest([_user("x")], turn_index=1, session_id="s1")
    assert store.get(first.added[0]).scene == ""

    svc.write_note("项目用 uv 管理依赖", kind="fact", scene="构建")

    assert store.get(first.added[0]).scene == "构建"
    assert len(store.active()) == 1


@pytest.mark.asyncio
async def test_r11c_candidates_filter_scene_before_ranking(tmp_path):
    """The R10 bug, scene edition: a filter that runs after ranking returns nothing."""
    store = MemoryStore(tmp_path / "m.json")
    # Both labels need three notes: a label that thin is not a group, so M8 drops
    # it instead of filtering (see the ignored-filter tests below).  The subject
    # here is ordering, not the label, so both sides are made into real groups —
    # if the filter ran after ranking, the top-1 would be one of the four
    # identical 构建 notes, not n4.
    for index, scene in enumerate(("构建", "构建", "构建", "构建", "发布", "发布", "发布")):
        store.add_note(
            MemoryNote(id=f"n{index}", text=f"依赖管理用 uv 方式{index}", scene=scene,
                       created_time="2026-09-25T00:00:00+00:00")
        )
    svc = MemoryService(store, config=_cfg())

    hits, diagnostics = await svc.recall_with_diagnostics(
        "依赖管理 uv", k=1, scene="发布"
    )

    assert [hit.note.id for hit in hits] == ["n4"]


@pytest.mark.asyncio
async def test_r11c_consolidate_does_not_merge_across_scenes(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="项目用 uv 管理依赖", kind=NoteKind.FACT,
                              scene="构建"))
    store.add_note(MemoryNote(id="b", text="项目用 uv 管理依赖，不要用 pip",
                              kind=NoteKind.FACT, scene="发布"))
    svc = MemoryService(store, config=_cfg(dedupe_similarity=0.6))

    report = await svc.consolidate(force=True)

    assert report.merged == ()
    assert len(store.active()) == 2


def test_r11d_a_service_without_config_ranks_with_blend(tmp_path):
    """`_default_config()` is what every config-less service reads, so R11d's two
    keys have to live there too — otherwise the default path would raise or
    silently miss the switch."""
    from ai_coding.memory.relevance import RecallParams

    params = RecallParams.from_memory_config(_default_config())

    assert params.recall_fusion == "blend"
    assert params.rrf_k == 60

    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store)  # no config at all
    svc.write_note("构建用 uv 管理依赖", kind="decision")
    hits, _diagnostics = asyncio.run(svc.recall_with_diagnostics("构建用什么依赖", k=3))
    assert [hit.note.id for hit in hits]


# ---- M8: a scene is only a group once enough memories share it -------------
#
# Measured on the real store: 43 notes, 36 labels, 30 of them singletons. The
# label is chosen by the extractor from an *open* vocabulary, so its count does
# not converge (Heaps' law — arxiv.org/pdf/0704.3316): the shape to aim for is
# a governed head separated from an ungoverned tail, not a fixed list. Support
# — how many notes carry the label — is the evidence, computed on read, so
# nothing is stored and nothing migrates.


def _scene_service(tmp_path, *scenes):
    from ai_coding.memory.service import MemoryService

    store = MemoryStore(tmp_path / "m.json")
    for index, scene in enumerate(scenes):
        store.add_note(MemoryNote(id=f"n{index}", text=f"note {index}", scene=scene))
    return MemoryService(store, config=_cfg())


def test_a_label_needs_three_memories_before_it_counts_as_a_group(tmp_path):
    svc = _scene_service(
        tmp_path, "项目", "项目", "项目", "环境", "环境", "环境", "文档"
    )

    summary = svc.index_summary()

    assert set(summary.governed_scenes) == {"项目", "环境"}
    assert "文档" not in summary.governed_scenes, "a one-note label is not a group"


def test_the_head_follows_the_data_so_a_label_is_promoted_by_being_used(tmp_path):
    """The point of an open vocabulary: nothing has to be registered. A label
    with one note today enters the head the day a second note joins it."""
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="one", scene="记忆"))
    svc = MemoryService(store, config=_cfg())
    assert svc.index_summary().governed_scenes == ()

    for index in range(2):
        store.add_note(MemoryNote(id=f"b{index}", text=f"more {index}", scene="记忆"))

    assert svc.index_summary().governed_scenes == ("记忆",)


def test_a_store_with_no_repeated_label_has_no_groups_and_says_nothing(tmp_path):
    svc = _scene_service(tmp_path, "甲", "乙", "丙")

    summary = svc.index_summary()

    assert summary.governed_scenes == ()
    # The raw counts stay available: the label is still on every note, it just
    # is not offered as a group.
    assert summary.by_scene == {"甲": 1, "乙": 1, "丙": 1}


@pytest.mark.asyncio
async def test_narrowing_by_a_one_note_label_is_ignored_and_explained(tmp_path):
    """Measured on the real store: 67% of notes sat on a scene nothing else
    shared, so `scene=` could only ever return that single note — strictly worse
    than not filtering, because it throws the rest of the index away. So a
    label that is not a group may not narrow: the filter is dropped and the
    caller is told why, instead of the model silently losing recall."""
    from ai_coding.memory.service import MemoryService

    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="the user prefers pytest", scene="酒店管理系统"))
    store.add_note(MemoryNote(id="b", text="the user prefers ruff", scene="酒店管理系统"))
    for index in range(3):
        store.add_note(MemoryNote(id=f"e{index}", text=f"env note {index}", scene="环境"))
    svc = MemoryService(store, config=_cfg())
    history = [_user("which linter does the user prefer?")]

    kept, diag = await svc.recall_with_diagnostics(history, scene="酒店管理系统")

    assert "pytest" in kept[0].note.text, "the lone label must not narrow to one note"
    assert diag.ignored_scene == "酒店管理系统"
    assert "ruff" in "".join(hit.note.text for hit in kept)


@pytest.mark.asyncio
async def test_narrowing_by_a_real_group_still_narrows(tmp_path):
    from ai_coding.memory.service import MemoryService

    store = MemoryStore(tmp_path / "m.json")
    for index in range(3):
        store.add_note(MemoryNote(id=f"e{index}", text=f"env note {index}", scene="环境"))
    store.add_note(MemoryNote(id="x", text="the user prefers pytest", scene="酒店管理系统"))
    svc = MemoryService(store, config=_cfg())
    history = [_user("note?")]

    kept, diag = await svc.recall_with_diagnostics(history, scene="环境")

    assert diag.ignored_scene is None
    assert kept and {hit.note.scene for hit in kept} == {"环境"}
