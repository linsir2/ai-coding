"""TDD (M1-2): SessionService — create/save/load/list/delete + atomic persistence."""

from ai_coding.domain.message import ChatMessage
from ai_coding.service.session_service import SessionService


def _svc(root) -> SessionService:
    return SessionService(root=root)


def test_create_session_fields(tmp_path):
    svc = _svc(tmp_path / "sessions")
    s = svc.create(title="hello")
    assert s.session_id
    assert s.title == "hello"
    assert s.created_time
    assert s.last_access_time
    assert s.messages == []


def test_save_load_roundtrip(tmp_path):
    svc = _svc(tmp_path / "sessions")
    s = svc.create(title="t")
    s.add_message(ChatMessage(role="user", content="hi", timestamp="ts1"))
    svc.save(s)
    loaded = svc.load(s.session_id)
    assert loaded is not None
    assert loaded.session_id == s.session_id
    assert loaded.title == "t"
    assert len(loaded.messages) == 1
    assert loaded.messages[0].content == "hi"
    assert loaded.messages[0].role == "user"


def test_load_missing_none(tmp_path):
    svc = _svc(tmp_path / "sessions")
    assert svc.load("nope") is None


def test_persist_creates_json_at_root(tmp_path):
    root = tmp_path / "sessions"
    svc = _svc(root)
    s = svc.create()
    svc.save(s)
    assert (root / f"{s.session_id}.json").is_file()


def test_no_leftover_tmp_files(tmp_path):
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    svc.save(s)
    leftovers = [p for p in (tmp_path / "sessions").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_list_empty_then_nonempty(tmp_path):
    svc = _svc(tmp_path / "sessions")
    assert svc.list_sessions() == []
    a = svc.create()
    b = svc.create()
    svc.save(a)
    svc.save(b)
    ids = {s.session_id for s in svc.list_sessions()}
    assert ids == {a.session_id, b.session_id}


def test_list_sessions_excludes_messages(tmp_path):
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    s.add_message(ChatMessage(role="user", content="hi", timestamp="ts"))
    svc.save(s)
    listed = svc.list_sessions()
    assert len(listed) == 1
    assert listed[0].messages == []


def test_list_sessions_preserves_first_user_message_preview(tmp_path):
    """The picker needs enough content for a user to recognize a session."""
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    s.add_message(ChatMessage(role="assistant", content="Working on it.", timestamp="ts"))
    s.add_message(
        ChatMessage(
            role="user",
            content="First saved question\nwith a newline   and extra spaces.",
            timestamp="ts",
        )
    )
    svc.save(s)
    listed = svc.list_sessions()

    assert getattr(listed[0], "first_preview", "") == (
        "First saved question with a newline and extra spaces."
    )


def test_preview_skips_the_compaction_checkpoint(tmp_path):
    """After /compact the first user-role row is the checkpoint note, not user
    content.  Measured live: the session header showed the whole summary
    single-lined, because the preview took history[0] verbatim."""
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    s.add_message(
        ChatMessage(
            role="user",
            content="[以下为早期对话摘要]\n## 目标\n排查 /compact 指令执行失败原因",
            timestamp="ts",
        )
    )
    s.add_message(ChatMessage(role="user", content="第二轮的问题", timestamp="ts"))
    svc.save(s)

    listed = svc.list_sessions()

    assert getattr(listed[0], "first_preview", "") == "第二轮的问题"


def test_preview_is_capped_because_a_preview_is_one_line(tmp_path):
    """A long first message must not blow up the header — the checkpoint bug
    exposed that previews had no bound at all."""
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    s.add_message(ChatMessage(role="user", content="字" * 300, timestamp="ts"))
    svc.save(s)

    listed = svc.list_sessions()

    preview = getattr(listed[0], "first_preview", "")
    assert len(preview) == 80


def test_preview_of_a_checkpoint_only_session_is_empty(tmp_path):
    """A history that is nothing but the checkpoint has no user content to
    preview; showing the summary again would recreate the defect."""
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    s.add_message(
        ChatMessage(role="user", content="[以下为早期对话摘要]\n## 目标\n无", timestamp="ts")
    )
    svc.save(s)

    listed = svc.list_sessions()

    assert getattr(listed[0], "first_preview", "") == ""


def test_list_sessions_reports_the_real_message_count(tmp_path):
    """The list view drops bodies, but the count has to stay truthful.

    ``/resume`` printed ``msgs=0`` for every session because it counted
    ``.messages`` of an already-stripped summary.
    """
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    for i in range(5):
        s.add_message(ChatMessage(role="user", content=f"m{i}", timestamp="ts"))
    svc.save(s)
    listed = svc.list_sessions()
    assert len(listed) == 1
    assert listed[0].messages == []  # bodies still dropped
    assert listed[0].message_count == 5  # but the count is real


def test_list_view_fields_are_not_persisted(tmp_path):
    """``to_dict`` is a whitelist that stays byte-compatible with the Java side.

    ``message_count`` and ``first_preview`` are computed while a session file is
    being read for the list view; if either reached the disk the saved format
    would change under the Java reader.
    """
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    s.add_message(ChatMessage(role="user", content="hi", timestamp="ts"))
    svc.save(s)
    svc.list_sessions()  # populates both fields
    on_disk = (tmp_path / "sessions" / f"{s.session_id}.json").read_text(encoding="utf-8")
    for leaked in ("messageCount", "message_count", "firstPreview", "first_preview"):
        assert leaked not in on_disk
    assert svc.load(s.session_id).to_dict() == s.to_dict()


def test_a_full_load_leaves_the_count_unknown(tmp_path):
    """``load`` returns the real bodies, so -1 ("not computed") is the honest value."""
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    s.add_message(ChatMessage(role="user", content="hi", timestamp="ts"))
    svc.save(s)
    loaded = svc.load(s.session_id)
    assert loaded.message_count == -1
    assert len(loaded.messages) == 1


def test_list_sessions_count_is_zero_for_an_empty_session(tmp_path):
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    svc.save(s)
    assert svc.list_sessions()[0].message_count == 0


def test_list_sessions_skips_broken_files_without_counting_them(tmp_path):
    svc = _svc(tmp_path / "sessions")
    good = svc.create()
    good.add_message(ChatMessage(role="user", content="hi", timestamp="ts"))
    svc.save(good)
    (tmp_path / "sessions" / "broken.json").write_text("{not json", encoding="utf-8")
    listed = svc.list_sessions()
    assert [s.session_id for s in listed] == [good.session_id]
    assert listed[0].message_count == 1


def test_resolve_finds_a_unique_prefix(tmp_path):
    """The list shows 12 hex chars; loading must accept what the list shows."""
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    svc.save(s)
    prefix = s.session_id[:12]
    assert [m.session_id for m in svc.resolve(prefix)] == [s.session_id]


def test_resolve_accepts_the_full_id(tmp_path):
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    svc.save(s)
    assert [m.session_id for m in svc.resolve(s.session_id)] == [s.session_id]


def test_resolve_returns_every_match_for_an_ambiguous_prefix(tmp_path):
    """Silently picking the first match would resume the wrong conversation."""
    svc = _svc(tmp_path / "sessions")
    ids = []
    for _ in range(2):
        s = svc.create()
        svc.save(s)
        ids.append(s.session_id)
    shared = "a"
    for name in ids:
        target = tmp_path / "sessions" / f"{name}.json"
        data = target.read_text(encoding="utf-8")
        new = shared * 8 + name[8:]
        target.unlink()
        (tmp_path / "sessions" / f"{new}.json").write_text(
            data.replace(name, new), encoding="utf-8"
        )
    assert len(svc.resolve(shared * 8)) == 2


def test_resolve_returns_nothing_for_an_unknown_prefix(tmp_path):
    svc = _svc(tmp_path / "sessions")
    svc.save(svc.create())
    assert svc.resolve("f" * 32) == []


def test_resolve_on_a_missing_root_is_empty(tmp_path):
    assert _svc(tmp_path / "nope").resolve("a" * 12) == []


def test_load_still_prefers_an_exact_hit(tmp_path):
    """Prefix matching must not change what an exact id does."""
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    svc.save(s)
    assert svc.load(s.session_id).session_id == s.session_id
    assert svc.load(s.session_id[:12]).session_id == s.session_id
    assert svc.load("0" * 32) is None


def test_delete(tmp_path):
    svc = _svc(tmp_path / "sessions")
    s = svc.create()
    svc.save(s)
    assert svc.load(s.session_id) is not None
    assert svc.delete(s.session_id) is True
    assert svc.load(s.session_id) is None
    assert svc.delete("absent") is False


def test_generate_unique_ids(tmp_path):
    svc = _svc(tmp_path / "sessions")
    ids = {svc.create().session_id for _ in range(20)}
    assert len(ids) == 20


def test_list_sessions_sorts_newest_first(tmp_path):
    """The resume picker must lead with the conversation just left, not a
    filename lottery: ``list_sessions`` is newest-``last_access_time`` first,
    with missing/garbled timestamps sunk to the bottom instead of raising."""
    from ai_coding.domain.session import SessionData

    svc = _svc(tmp_path / "sessions")
    old = SessionData(
        session_id="0" * 32, title="", created_time="2026-09-01T00:00:00Z",
        last_access_time="2026-09-01T00:00:00Z",
    )
    new = SessionData(
        session_id="f" * 32, title="", created_time="2026-10-02T00:00:00Z",
        last_access_time="2026-10-02T00:00:00Z",
    )
    timeless = SessionData(
        session_id="a" * 32, title="", created_time="", last_access_time="garbage"
    )
    for s in (old, new, timeless):
        svc.save(s)

    assert [s.session_id for s in svc.list_sessions()] == [
        new.session_id, old.session_id, timeless.session_id
    ]


def test_load_with_mismatched_filename_and_field_id_returns_none(tmp_path):
    """L1: a file whose name and sessionId field disagree must be an honest
    miss, not a load resolving itself forever."""
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    (root / "aa.json").write_text(
        '{"sessionId": "bb", "title": "", "createdTime": "",'
        ' "lastAccessTime": "", "messages": []}',
        encoding="utf-8",
    )
    assert _svc(root).load("bb") is None


def test_hostile_session_ids_are_refused_loudly(tmp_path):
    """L3: path separators must never reach the filesystem -- a loud error,
    not a miss that a caller could mistake for absent."""
    import pytest

    svc = _svc(tmp_path / "sessions")
    with pytest.raises(ValueError):
        svc.load("../../x")
    with pytest.raises(ValueError):
        svc.delete("../../x")
