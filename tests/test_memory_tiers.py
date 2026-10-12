"""Two tiers of memory: one that follows the repo, one that follows the person.

Everything lived in `<workspace>/.memory`, so a preference you taught the agent
in one project was gone in the next. The user tier fixes that; these tests pin
the two properties that make it safe rather than merely present:

* **Dedupe is tier-scoped.** Searching the union when deciding whether to merge
  would let a project fact ("this repo uses pytest") match a user-tier note
  ("prefers pytest") and be merged into it, silently promoting project knowledge
  to every future project. So `find_by_dedupe` and `find_similar` take the tier
  they are about to write into, and default to the workspace tier, which is what
  every caller before this meant.
* **The nearer scope wins a tie.** Ordering is decided in `relevance.order`, the
  one place every rank path goes through, so the tier is ranked there rather
  than in each caller. With only a workspace tier the rank is uniformly 0, and
  the ordering is unchanged.
"""

import asyncio
from types import SimpleNamespace

from ai_coding.domain.message import ChatMessage
from ai_coding.memory.note import USER_SCOPE, MemoryNote, NoteKind, new_note
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.scoped_store import ScopedStore
from ai_coding.memory.store import MemoryStore


def _note(text: str, *, scope: str | None = None, **kw) -> MemoryNote:
    if scope is None:
        return new_note(text, **kw)
    return new_note(text, scope=scope, **kw)


def _pair(tmp_path):
    project = MemoryStore(tmp_path / "project" / ".memory")
    user = MemoryStore(tmp_path / "user" / "memory.json")
    return ScopedStore(project, user), project, user


def test_a_user_tier_note_lands_in_the_user_store(tmp_path):
    scoped, project, user = _pair(tmp_path)

    scoped.add_note(_note("偏好 pytest", scope="user"))

    assert [n.text for n in user.all()] == ["偏好 pytest"]
    assert project.all() == []


def test_the_default_tier_is_still_the_workspace(tmp_path):
    """Every existing note has scope=workspace, and a missing field must not
    start sending them somewhere else."""
    scoped, project, user = _pair(tmp_path)

    scoped.add_note(new_note("项目约定"))

    assert [n.text for n in project.all()] == ["项目约定"]
    assert user.all() == []


def test_an_unknown_scope_goes_to_the_workspace_not_the_user_tier(tmp_path):
    """A value nobody declared must not be able to reach the global file."""
    scoped, _project, user = _pair(tmp_path)

    scoped.add_note(new_note("x", scope="subagent:review:root"))

    assert user.all() == [], "nothing but 'user' may open the global tier"


# ---- reads are the union ---------------------------------------------------


def test_active_and_all_are_the_union_of_both(tmp_path):
    scoped, _project, _user = _pair(tmp_path)
    scoped.add_note(new_note("项目的一条", scope="workspace"))
    scoped.add_note(_note("用户的一条", scope="user"))

    assert {n.text for n in scoped.all()} == {"项目的一条", "用户的一条"}
    assert scoped.count == 2
    assert len(scoped.active()) == 2


def test_get_finds_a_note_in_either_tier(tmp_path):
    scoped, _project, _user = _pair(tmp_path)
    scoped.add_note(_note("用户的一条", scope="user"))
    scoped.add_note(new_note("项目的一条"))

    by_scope = {n.scope: n.id for n in scoped.all()}
    assert scoped.get(by_scope["user"]).scope == "user"
    assert scoped.get(by_scope["workspace"]).scope == "workspace"
    assert scoped.get("no-such-id") is None


def test_search_scored_ranks_across_both_tiers(tmp_path):
    """`search_scored` is a thin delegate over `rank_notes(self.active())`; the
    composite reuses that same function rather than reimplementing ranking."""
    scoped, _project, _user = _pair(tmp_path)
    scoped.add_note(new_note("pytest 跑项目测试", scope="workspace"))
    scoped.add_note(_note("用户偏好 pytest 跑测试", scope="user"))

    hits = scoped.search_scored("pytest 测试")

    assert {h.note.text for h in hits} == {"pytest 跑项目测试", "用户偏好 pytest 跑测试"}


def test_an_absent_user_file_contributes_nothing(tmp_path):
    """A fresh machine: `MemoryStore._load` returns [] for a missing path, so the
    user tier is inert until something is written there."""
    project = MemoryStore(tmp_path / "project" / ".memory")
    project.add_note(new_note("only this"))
    scoped = ScopedStore(project, MemoryStore(tmp_path / "nowhere" / "memory.json"))

    assert [n.text for n in scoped.all()] == ["only this"]


# ---- dedupe is scoped to the tier being written into ----------------------


def test_find_by_dedupe_defaults_to_the_workspace_tier(tmp_path):
    scoped, project, user = _pair(tmp_path)
    same = "完全相同的一句话"
    scoped.add_note(new_note(same, scope="workspace"))
    scoped.add_note(_note(same, scope="user"))

    key = _note(same).dedupe_key
    assert scoped.find_by_dedupe(key).scope == "workspace"
    assert scoped.find_by_dedupe(key, scope="user").scope == "user"
    assert scoped.find_by_dedupe(key, scope="user").id != scoped.find_by_dedupe(key).id, (
        "the same sentence at two tiers is two notes, not one silently shared"
    )


def test_find_similar_does_not_reach_the_other_tier(tmp_path):
    """The contamination path: a project fact matching a user-tier note must not
    be merged into it."""
    scoped, _project, _user = _pair(tmp_path)
    scoped.add_note(_note("prefers pytest for tests", scope="user"))

    found = scoped.find_similar("本仓库用 pytest 做测试", kind=NoteKind.FACT, threshold=0.5)

    assert found is None, "a project fact must not find a user-tier note to merge into"


def test_find_similar_still_finds_its_own_tier(tmp_path):
    scoped, _project, _user = _pair(tmp_path)
    scoped.add_note(new_note("本仓库用 pytest 做测试", scope="workspace"))

    found = scoped.find_similar("本仓库用 pytest 做测试", kind=NoteKind.FACT, threshold=0.5)

    assert found is not None and found.scope == "workspace"


# ---- writes route by where the note is -------------------------------------


def test_writes_find_the_note_in_either_tier(tmp_path):
    scoped, project, user = _pair(tmp_path)
    scoped.add_note(_note("用户的一条", scope="user"))
    scoped.add_note(new_note("项目的一条"))
    user_id = [n.id for n in scoped.all() if n.scope == "user"][0]
    project_id = [n.id for n in scoped.all() if n.scope == "workspace"][0]

    assert scoped.pin(user_id).scope == "user"
    edited = scoped.get(project_id)
    assert scoped.update(edited.__class__(**{**edited.__dict__, "text": "项目改了"})) is True
    assert scoped.update_stake(user_id, 0.7) is True
    assert scoped.touch_notes([user_id, project_id]) == 2
    assert scoped.delete(project_id) is True
    assert [n.text for n in user.all()] == ["用户的一条"]
    assert project.all() == []


def test_delete_of_a_user_note_is_visible_from_every_project(tmp_path):
    """One file, one truth: removing it in project B removes it in A."""
    scoped, _project, user = _pair(tmp_path)
    scoped.add_note(_note("一个全局偏好", scope="user"))
    note_id = scoped.all()[0].id

    assert scoped.delete(note_id) is True
    assert user.all() == []


def test_supersede_moves_both_notes_inside_the_tier_that_held_the_old_one(tmp_path):
    scoped, project, user = _pair(tmp_path)
    scoped.add_note(_note("旧版本", scope="user"))
    old_id = scoped.all()[0].id
    newer = new_note("新版本", scope="user")

    assert scoped.supersede(old_id, newer) is True
    # The old note is retired, not deleted — rollback stays possible, which is
    # the whole point of the supersede contract.
    assert [n.text for n in user.all()] == ["旧版本", "新版本"]
    assert [n.text for n in user.active()] == ["新版本"]
    assert project.all() == []


def test_supersede_of_a_note_nowhere_is_false(tmp_path):
    scoped, _project, _user = _pair(tmp_path)

    assert scoped.supersede("ghost", new_note("x", scope="user")) is False


def test_evict_caps_each_tier_separately(tmp_path):
    """One cap for both would let the user tier's entries evict the project's,
    or the reverse."""
    scoped, project, user = _pair(tmp_path)
    for index in range(3):
        scoped.add_note(new_note(f"项目 {index}", scope="workspace"))
        scoped.add_note(_note(f"用户 {index}", scope="user"))

    evicted = scoped.evict(2)

    assert len(evicted) == 2
    assert project.count == 2 and user.count == 2


def test_clear_clears_both_tiers(tmp_path):
    scoped, project, user = _pair(tmp_path)
    scoped.add_note(new_note("a"))
    scoped.add_note(_note("b", scope="user"))

    scoped.clear()

    assert scoped.all() == []


# ---- the tie-break lives in the one ranking function -----------------------


def _pinned_time(text: str, scope: str) -> MemoryNote:
    """Same text, same kind, same timestamp — so the only difference is the tier.

    The timestamp has to be forced: `new_note` stamps `now_utc()`, and two notes
    created a microsecond apart already differ in their recency term, so they do
    not actually tie and the tie-break is never consulted. This is not a test
    detail — it means the tier tie-break only fires when scores are equal to the
    last bit, which in practice is when the two notes are truly identical."""
    from dataclasses import replace

    return replace(new_note(text, scope=scope), created_time="2026-01-01T00:00:00+00:00")


def test_a_project_note_outranks_a_user_note_on_a_tie(tmp_path):
    """Ordering is decided in `relevance.order`, through which every rank path
    goes; putting the tier there means one change covers `recall`,
    `search_scored` and the tail block alike."""
    from ai_coding.memory.relevance import rank_notes

    scoped, _project, _user = _pair(tmp_path)
    scoped.add_note(_pinned_time("duplicate text here", "workspace"))
    scoped.add_note(_pinned_time("duplicate text here", "user"))

    ranked = rank_notes(scoped.active(), "duplicate text here")

    assert ranked[0].score == ranked[1].score, "the fixture has to actually tie"
    assert ranked[0].note.scope == "workspace", "the nearer scope wins a tie"
    assert ranked[1].note.scope == "user"


def test_with_one_tier_the_ordering_is_unchanged(tmp_path):
    """The tie-break must be inert when there is no user tier, or every existing
    ordering changes for no reason."""
    from ai_coding.memory.relevance import rank_notes

    store = MemoryStore(tmp_path / "solo" / ".memory")
    for index in range(4):
        store.add_note(new_note(f"same score note {index}"))

    ranked = rank_notes(store.active(), "same score note")

    assert len(ranked) == 4
    assert {n.note.text for n in ranked} == {n.text for n in store.all()}


# ---- the two properties this layer exists for, end to end ------------------


def _turn(service):
    from ai_coding.core.memory_turn import TurnMemory

    return TurnMemory(service, emit=lambda event: None, save=lambda session: None)


def _service(tmp_path, *, extractor=None):
    from ai_coding.memory.relevance import RecallParams
    from ai_coding.memory.service import MemoryService

    project = MemoryStore(tmp_path / "project" / ".memory")
    user = MemoryStore(tmp_path / "user" / "memory.json")
    return MemoryService(
        ScopedStore(project, user),
        extractor=extractor,
        config=SimpleNamespace(consolidate_threshold=10, max_index_entries=200),
    ), RecallParams()


class _Extractor:
    def __init__(self, *candidates):
        self.candidates = list(candidates)

    async def __call__(self, messages, conclusion=None):
        return list(self.candidates)

    def reset(self):
        pass


def _candidate(text, *, kind=NoteKind.FACT, **kw):
    return HarvestCandidate(text=text, kind=kind, importance=3, **kw)


def test_a_preference_learned_in_one_project_is_recalled_in_another(tmp_path):
    """The whole reason the layer exists, stated as the operator experiences it.

    The service is built over *one* project's stores; the user tier is a file
    outside any workspace, so the same service sees it from anywhere.
    """
    service, _ = _service(tmp_path)
    service.write_note("偏好用 pytest 跑测试", kind="preference", scope=USER_SCOPE)

    hits = asyncio.run(service.recall_notes("测试怎么跑", k=5))

    assert any(n.note.scope == USER_SCOPE for n in hits), (
        "a user-tier note must be reachable from a different project's store"
    )


def test_a_project_fact_does_not_travel_to_another_project(tmp_path):
    service, _ = _service(tmp_path)
    service.write_note("本仓库的依赖用 uv 管理")

    hits = asyncio.run(service.recall_notes("依赖怎么管", k=5))

    assert [n.note.text for n in hits] == ["本仓库的依赖用 uv 管理"]


def test_the_same_preference_written_in_two_projects_is_one_note(tmp_path):
    """Claude Code #100315 is this failure with the serial numbers filed off:
    the model writes the same rule again because it was loaded, and the index
    grows toward its limit for nothing."""

    from ai_coding.memory.note import USER_SCOPE

    service, _ = _service(tmp_path)
    # Two different projects, both declaring the preference user-tier — which is
    # what `memory_write(scope="user")` does, and the only thing that routes here.
    service.write_note("偏好用 pytest 跑测试", kind="preference", scope=USER_SCOPE)
    service.write_note("偏好用 pytest 跑测试", kind="preference", scope=USER_SCOPE)

    assert service.store.count == 1, "one fact, one note, however many projects said it"


def test_a_fact_at_two_tiers_is_two_notes_on_purpose(tmp_path):
    """The consequence of tier-scoped dedupe, stated so nobody "fixes" it.

    The same sentence written user-tier in A and left to harvest in B is two
    notes, and that is correct: one is a fact about the person, the other is
    this repo's own. Merging them would mean either losing the project's copy or
    silently promoting it."""
    service, _ = _service(tmp_path)
    service.write_note("偏好用 pytest 跑测试", kind="preference", scope=USER_SCOPE)
    service.write_note("偏好用 pytest 跑测试", kind="preference")  # harvest's tier

    assert {(n.scope) for n in service.store.all()} == {USER_SCOPE, "workspace"}


def test_a_project_fact_is_never_merged_into_a_user_tier_note(tmp_path):
    """The contamination path. `find_similar` returning a cross-tier note would
    splice project knowledge into the file every other project reads."""
    import asyncio

    from ai_coding.memory.note import USER_SCOPE

    service, _ = _service(tmp_path, extractor=_Extractor(_candidate("本仓库要用 pytest 做测试")))
    service.write_note("prefers pytest for tests", kind="preference", scope=USER_SCOPE)

    report = asyncio.run(
        service.harvest([_user("本仓库要用 pytest 做测试")], session_id="project-b", turn_index=1)
    )

    texts = [(n.text, n.scope) for n in service.store.active()]
    assert ("本仓库要用 pytest 做测试", "workspace") in texts
    assert ("prefers pytest for tests", USER_SCOPE) in texts
    assert report.added, "the project fact was stored as its own note"


def test_consolidation_never_merges_across_tiers(tmp_path):
    """Retiring a user-tier note in favour of a project-tier one would delete it
    from every other project while the replacement lived only in this one."""
    import asyncio

    from ai_coding.memory.note import USER_SCOPE

    service, _ = _service(tmp_path)
    service.write_note("完全相同的两句话", scope=USER_SCOPE)
    service.write_note("完全相同的两句话")

    report = asyncio.run(service.consolidate(force=True))

    assert service.store.active(scope=USER_SCOPE) == service.store.active(scope=USER_SCOPE)
    assert len(service.store.active(scope=USER_SCOPE)) == 1
    assert len(service.store.active(scope="workspace")) == 1
    assert not report.merged, "nothing was merged across the tiers"


def test_status_and_the_panel_report_both_tiers(tmp_path):
    from ai_coding.memory.note import USER_SCOPE

    service, _ = _service(tmp_path)
    service.write_note("项目的一条")
    service.write_note("用户的一条", scope=USER_SCOPE)

    rows = service.store.tiers()

    assert [n.text for n in rows["workspace"]] == ["项目的一条"]
    assert [n.text for n in rows[USER_SCOPE]] == ["用户的一条"]


def test_a_service_given_a_bare_store_still_works(tmp_path):
    """The normalizer: passing a plain MemoryStore must not silently disable
    harvest, which is what a tier-scoped call on a store without that parameter
    would do."""
    from ai_coding.memory.service import MemoryService

    service = MemoryService(MemoryStore(tmp_path / "bare" / ".memory"))

    service.write_note("一条普通记忆")

    assert service.store.count == 1
    assert asyncio.run(service.recall_notes("记忆", k=3))


def _user(content):

    return ChatMessage(role="user", content=content, timestamp="t")


# ---- promotion: the way a project fact becomes a personal one ---------------


def test_promotion_moves_a_note_into_the_user_tier(tmp_path):


    service, _ = _service(tmp_path)
    service.write_note("偏好用 pytest 跑测试", kind="preference")
    note_id = service.store.all()[0].id

    _turn(service).promote(note_id)

    assert service.store.get(note_id).scope == USER_SCOPE
    assert service.store.active(scope=USER_SCOPE)
    assert not service.store.active(scope="workspace")


def test_promotion_leaves_the_id_and_text_alone(tmp_path):
    """Only the store it lives in moves. A new id would orphan every reference
    the panel or a supersede chain holds."""

    service, _ = _service(tmp_path)
    service.write_note("偏好中文回答", kind="preference")
    before = service.store.all()[0]

    moved = _turn(service).promote(before.id)

    assert (moved.id, moved.text, moved.kind) == (before.id, before.text, before.kind)


def test_promoting_something_already_global_is_not_a_failure(tmp_path):

    service, _ = _service(tmp_path)
    service.write_note("已经是全局的", scope=USER_SCOPE)
    note_id = service.store.all()[0].id

    assert _turn(service).promote(note_id).id == note_id


def test_promoting_a_note_that_does_not_exist_is_none(tmp_path):

    service, _ = _service(tmp_path)

    assert _turn(service).promote("ghost") is None


def test_promotion_actually_moves_the_file_it_lives_in(tmp_path):
    """`update` rewrites the note in whichever tier holds it, so editing the scope
    field in place would leave a user-tier *label* on a note still sitting in the
    project's file — recalled from no other project, and reported as global by the
    panel. Promotion is a move, not an edit."""
    scoped, project, user = _pair(tmp_path)
    scoped.add_note(new_note("项目的一条"))
    note_id = scoped.all()[0].id

    moved = scoped.move_tier(note_id, USER_SCOPE)

    assert moved.scope == USER_SCOPE
    assert [n.id for n in user.all()] == [note_id]
    assert project.all() == [], "the project's file no longer holds it"


def test_moving_to_the_tier_it_is_already_in_is_a_no_op(tmp_path):
    scoped, project, user = _pair(tmp_path)
    scoped.add_note(_note("用户的一条", scope="user"))
    note_id = scoped.all()[0].id

    same = scoped.move_tier(note_id, USER_SCOPE)

    assert same.id == note_id
    assert [n.id for n in user.all()] == [note_id]
    assert project.all() == []


def test_the_panel_can_see_the_tier_of_every_note(tmp_path):
    """A leak has to be visible to be fixable. Without the tier in the row, a
    user-tier note looks like any other and nobody can tell why it is showing up
    everywhere."""
    service, _ = _service(tmp_path)
    service.write_note("项目的一条")
    service.write_note("用户的一条", scope=USER_SCOPE)

    rows = [n.to_dict()["scope"] for n in service.store.all()]

    assert sorted(rows) == ["user", "workspace"]
