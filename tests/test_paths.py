"""The two tiers: a user-global one that follows the machine, a workspace one
that follows the repo. Everything the app derives comes from here, resolved
once."""

import os

import pytest

from ai_coding.paths import APP_DIR, Paths, config_home, data_home


def test_the_config_home_follows_xdg_then_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert config_home() == tmp_path / "xdg-config" / APP_DIR

    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    assert config_home() == tmp_path / ".config" / APP_DIR


def test_the_data_home_follows_xdg_then_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert data_home() == tmp_path / "xdg-data" / APP_DIR

    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    assert data_home() == tmp_path / ".local" / "share" / APP_DIR


def test_both_tiers_live_under_the_same_app_directory(monkeypatch, tmp_path):
    """They are spelled out separately in two modules and used to be able to
    drift; one name, and the grants file and the token file cannot part ways."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))

    assert config_home().name == data_home().name == APP_DIR


def test_the_workspace_is_resolved_once(monkeypatch, tmp_path):
    """A relative ``-w`` used to leave every derived path at the mercy of the
    process CWD — which is how the embedding cache came to follow ``cd``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()

    paths = Paths.for_workspace("proj")

    assert paths.workspace == tmp_path / "proj"
    assert paths.memory == tmp_path / "proj" / ".memory"
    assert paths.sessions == tmp_path / "proj" / ".ai-coding" / "sessions"


def test_a_tilde_workspace_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    (tmp_path / "proj").mkdir()

    assert Paths.for_workspace("~/proj").workspace == (tmp_path / "proj").resolve()


def test_the_project_skill_library_beats_the_global_one(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths = Paths.for_workspace(tmp_path / "proj")

    assert paths.skill_roots[-1] == paths.project_skills
    assert paths.skill_roots[0] == paths.user_skills
    assert paths.project_skills != paths.user_skills


def test_the_workspace_tier_is_where_the_run_leaves_its_traces(monkeypatch, tmp_path):
    paths = Paths.for_workspace(tmp_path / "proj")

    assert paths.state.parent == paths.workspace
    for derived in (paths.sessions, paths.traces, paths.exports, paths.hooks, paths.memory):
        assert paths.workspace in derived.parents or derived.parent == paths.workspace


def test_the_global_tier_is_outside_the_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    paths = Paths.for_workspace(tmp_path / "proj")

    assert paths.workspace not in paths.user_skills.parents
    assert paths.workspace not in paths.user_config.parents


# ---- the two modules that used to spell this out themselves ---------------


def test_the_grants_file_is_the_one_this_module_names(monkeypatch, tmp_path):
    """`security/grants.py` had its own copy of the XDG expression; two copies
    of a path is how a file ends up somewhere nothing looks for it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    from ai_coding.security.grants import grants_path

    assert grants_path() == config_home() / "grants.json"


def test_the_mcp_token_file_is_the_one_this_module_names(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    from ai_coding.core.mcp import _auth_store_path

    assert _auth_store_path() == data_home() / "mcp-auth.json"


def test_no_duplicate_xdg_expressions_are_left_behind():
    """The point of the module. A second spelling is a second location."""
    import subprocess

    found = subprocess.run(
        ["grep", "-rn", "XDG_CONFIG_HOME\\|XDG_DATA_HOME", "ai_coding/"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)),
    ).stdout
    stragglers = [
        line for line in found.splitlines()
        if "ai_coding/paths.py" not in line
    ]
    assert not stragglers, "XDG lookup duplicated outside paths.py:\n" + "\n".join(stragglers)


@pytest.mark.parametrize("name", ["config_home", "data_home"])
def test_the_home_fallback_is_used_when_xdg_is_unset(monkeypatch, tmp_path, name):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))

    resolved = getattr(Paths.for_workspace(tmp_path / "proj"), name)

    fallback = tmp_path / ".config" if name == "config_home" else tmp_path / ".local" / "share"
    assert resolved == fallback / APP_DIR


def test_compat_roots_sit_between_user_and_project(monkeypatch, tmp_path):
    """r16/E-7: a repo that ships skills for other agents (Claude Code's
    .claude/skills, the .agents/skills convention) loads them unmodified —
    between the personal library and the repo's own skills/, so the native
    layout still speaks loudest."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = tmp_path / "proj"
    paths = Paths.for_workspace(workspace)

    assert paths.skill_roots == (
        paths.user_skills,
        workspace / ".claude" / "skills",
        workspace / ".agents" / "skills",
        paths.project_skills,
    )
