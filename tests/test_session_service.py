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
