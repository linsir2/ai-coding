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


def test_an_unwritable_store_does_not_raise(store, refused):
    """Best effort, like every other persistence in this project.  The *grant*
    is the gate's to keep for this session; all the store owes is to fail
    quietly rather than take the turn down with it."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    with refused(store.path, "write_text"):
        store.add("edit:a.py")  # must not raise

        assert not store.path.exists()


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


# ---- M-D3: TTL, expiry, prune, revoke, and the meta shape --------------------


def _clock_state(start: float = 1000.0) -> dict:
    return {"t": float(start)}


def test_an_expired_grant_is_not_loaded(tmp_path, monkeypatch):
    """U12: a grant written with a TTL stops being a grant once it is past."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    now = _clock_state()
    store = GrantStore(
        workspace=tmp_path / "w", clock=lambda: now["t"], ttl=60.0
    )
    store.add("edit:a.py")

    now["t"] += 61.0

    assert store.load() == set()
    assert store.load_with_expiry() == (set(), {})


def test_load_does_not_write_the_expired_grant_away(tmp_path, monkeypatch):
    """The reader is read-only: pruning is a write path's job, not load's."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    now = _clock_state()
    store = GrantStore(
        workspace=tmp_path / "w", clock=lambda: now["t"], ttl=60.0
    )
    store.add("edit:a.py")

    now["t"] += 61.0
    store.load()

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["workspaces"][str(store.workspace)] == ["edit:a.py"]
    assert document["meta"][str(store.workspace)]["edit:a.py"]["ttl"] == 60.0


def test_prune_drops_the_expired_grant_and_its_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    now = _clock_state()
    store = GrantStore(
        workspace=tmp_path / "w", clock=lambda: now["t"], ttl=60.0
    )
    store.add("edit:a.py")

    now["t"] += 61.0
    store.prune()

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["workspaces"][str(store.workspace)] == []
    assert str(store.workspace) not in document["meta"]


def test_revoke_removes_the_list_entry_and_the_meta(tmp_path, monkeypatch):
    """Both halves, so a revoked grant leaves no orphan expiry record."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = GrantStore(workspace=tmp_path / "w")
    store.add("edit:a.py")

    assert store.revoke("edit:a.py") is True

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["workspaces"][str(store.workspace)] == []
    assert str(store.workspace) not in document["meta"]


def test_revoke_of_an_unknown_key_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = GrantStore(workspace=tmp_path / "w")
    store.add("edit:a.py")

    assert store.revoke("edit:b.py") is False


def test_clear_deletes_the_meta_block_even_when_the_list_is_gone(
    tmp_path, monkeypatch
):
    """A workspace can hold a meta block with no list entry; clear empties both."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = GrantStore(workspace=tmp_path / "w")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {},
                "meta": {str(store.workspace): {"edit:a.py": {"grantedAt": 1.0, "ttl": 60.0}}},
            }
        ),
        encoding="utf-8",
    )

    store.clear()

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert str(store.workspace) not in document.get("meta", {})


def test_a_grant_without_a_meta_record_is_permanent(tmp_path, monkeypatch):
    """The v1 shape has no meta at all: every grant it holds is permanent."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = GrantStore(workspace=tmp_path / "w")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"version": 1, "workspaces": {str(store.workspace): ["edit:a.py"]}}),
        encoding="utf-8",
    )

    now = {"t": 1e12}
    live = GrantStore(workspace=tmp_path / "w", clock=lambda: now["t"])

    assert live.load() == {"edit:a.py"}
    assert live.load_with_expiry() == ({"edit:a.py"}, {})


def test_extra_meta_keys_are_ignored_and_a_missing_ttl_is_permanent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = GrantStore(workspace=tmp_path / "w")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {
                    str(store.workspace): ["edit:timed.py", "edit:forever.py"]
                },
                "meta": {
                    str(store.workspace): {
                        "edit:timed.py": {"grantedAt": 1000.0, "ttl": 60.0, "note": "x"},
                        "edit:forever.py": {"grantedAt": 1000.0, "note": "no ttl"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    now = {"t": 1000.0}
    live = GrantStore(workspace=tmp_path / "w", clock=lambda: now["t"])

    grants, expiry = live.load_with_expiry()
    assert grants == {"edit:timed.py", "edit:forever.py"}
    assert expiry == {"edit:timed.py": 1060.0}


def test_meta_for_a_key_the_list_does_not_hold_is_ignored(tmp_path, monkeypatch):
    """The list is authoritative; a stray meta record grants nothing."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = GrantStore(workspace=tmp_path / "w")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {str(store.workspace): ["edit:a.py"]},
                "meta": {str(store.workspace): {"edit:ghost.py": {"grantedAt": 1.0, "ttl": 60.0}}},
            }
        ),
        encoding="utf-8",
    )

    assert store.load() == {"edit:a.py"}


def test_reapproving_refreshes_the_grant_clock(tmp_path, monkeypatch):
    """'Always' again means the clock restarts, not a second row."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    now = _clock_state()
    store = GrantStore(
        workspace=tmp_path / "w", clock=lambda: now["t"], ttl=60.0
    )
    store.add("edit:a.py")

    now["t"] += 50.0
    store.add("edit:a.py")

    now["t"] += 50.0  # 100s since the first add, 50s since the second
    assert store.load() == {"edit:a.py"}


def test_a_disk_loaded_grant_expires_inside_the_session(tmp_path, monkeypatch):
    """U13: the TTL travels with a grant read from disk, and the gate reads the
    clock per call -- so the grant dies inside the session that loaded it."""
    from ai_coding.config.models import AppConfig
    from ai_coding.security.permission_gate import PermissionDecision, PermissionGate

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    now = _clock_state()
    clock = lambda: now["t"]  # noqa: E731
    GrantStore(workspace, clock=clock, ttl=60.0).add("edit:src/a.py")

    gate = PermissionGate.from_config(
        AppConfig(), workspace=str(workspace), clock=clock
    )

    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.ALLOW
    now["t"] += 61.0
    assert gate.check("edit", {"path": "src/a.py"}) == PermissionDecision.WARN
    assert gate.grants == ()
