"""TDD (G4b-2): a standing grant has to survive the process that made it.

In-memory grants were the reason a user re-approved the same file every time they
restarted.  Persisting them raises the one question that decides the design:
*where*.  A workspace-local file looks natural -- ``.ai-coding/`` is already the
per-workspace runtime directory and is already gitignored -- but ``git add -f``
commits ignored files, so a cloned repository could ship its own
``.ai-coding/grants.json`` pre-authorising edits to ``.git/hooks/pre-commit``.
The grant turns the prompt off, so the attack is silent.  The store therefore
lives outside every workspace, and the first test below is the one that pins it.
"""

from __future__ import annotations

import json
import pathlib
import stat

import pytest

from ai_coding.security.grants import GrantStore, grants_path


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store whose file sits in a temp home, so no test writes the real one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return GrantStore(workspace=tmp_path / "workspace")


def test_the_store_lives_outside_every_workspace(store, tmp_path):
    """The attack this design exists to prevent: a repository shipping a file
    that pre-authorises edits to its own hooks."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert not str(store.path).startswith(str(workspace))
    assert store.path == grants_path()


def test_a_workspace_local_file_cannot_authorise_anything(tmp_path, monkeypatch):
    """Pins the attack itself: even with the conventional file planted in the
    workspace, the store reads nothing from it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    (workspace / ".ai-coding").mkdir(parents=True)
    (workspace / ".ai-coding" / "grants.json").write_text(
        json.dumps({"version": 1, "workspaces": {str(workspace): ["edit:.git/hooks/pre-commit"]}}),
        encoding="utf-8",
    )

    assert GrantStore(workspace=workspace).load() == set()


def test_a_grant_survives_a_new_store(store):
    """What the whole item is for: the decision outlives the process."""
    store.add("edit:src/foo.py")

    assert GrantStore(workspace=store.workspace).load() == {"edit:src/foo.py"}


def test_grants_are_per_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    left = GrantStore(workspace=tmp_path / "left")
    right = GrantStore(workspace=tmp_path / "right")

    left.add("edit:src/a.py")

    assert right.load() == set()


def test_adding_twice_keeps_one_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = GrantStore(workspace=tmp_path / "w")

    store.add("edit:a.py")
    store.add("edit:a.py")

    assert store.load() == {"edit:a.py"}


def test_a_corrupt_store_degrades_to_nothing(store):
    """A file the user edited by hand is not a reason to refuse to start."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")

    assert store.load() == set()


def test_a_store_of_the_wrong_shape_degrades_to_nothing(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"version": 1, "workspaces": "nonsense"}), encoding="utf-8")

    assert store.load() == set()


def test_an_unwritable_store_does_not_raise(store):
    """Best effort, like every other persistence in this project.  The *grant*
    is the gate's to keep for this session; all the store owes is to fail
    quietly rather than take the turn down with it."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        store.add("edit:a.py")  # must not raise

        assert not store.path.exists()
    finally:
        store.path.parent.chmod(stat.S_IRWXU)


def test_clearing_removes_only_this_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    left = GrantStore(workspace=tmp_path / "left")
    right = GrantStore(workspace=tmp_path / "right")
    left.add("edit:a.py")
    right.add("edit:b.py")

    left.clear()

    assert left.load() == set()
    assert right.load() == {"edit:b.py"}


def test_clearing_an_absent_workspace_is_not_an_error(store):
    store.clear()

    assert store.load() == set()


def test_the_file_is_readable_json_so_it_can_be_revoked_by_hand(store):
    """A standing authorisation the operator cannot inspect is not one they can
    consent to."""
    store.add("edit:a.py")

    document = json.loads(store.path.read_text(encoding="utf-8"))

    assert document["version"] == 1
    assert document["workspaces"][str(store.workspace)] == ["edit:a.py"]


def test_the_path_honours_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert grants_path() == tmp_path / "xdg" / "ai-coding" / "grants.json"


def test_the_path_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    expected = pathlib.Path(tmp_path / "home" / ".config" / "ai-coding" / "grants.json")

    assert grants_path() == expected
