"""Layered config: a user-global base and a project overlay.

The reason this exists is one failure: with no project config file, the run
started on ``AppConfig()`` defaults, where ``default_model`` is ``""`` and the
engine cannot be built. So the API key had to be copied into every project as a
``config.local.yaml`` — which is why "change directory" read as "lose the
setup".

The layering is the same one Codex and Claude Code use for ``AGENTS.md``: a
user-level file plus a project-level one, both loaded, the nearer winning.
"""

import pytest

from ai_coding.config.loader import ConfigLoader
from ai_coding.paths import Paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_global_config_alone_is_enough_to_start(home):
    """The whole point: no per-project config file required."""
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "default_model: my-global-model\n"
        "models:\n  gpt-x:\n    name: gpt-x\n    base_url: https://example.test\n"
        "    api_key: sk-global\n",
    )

    app = ConfigLoader.load_layers(paths)

    assert app.default_model == "my-global-model"
    assert app.models["gpt-x"].api_key == "sk-global"


def test_the_project_overlays_the_global_one(home):
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "default_model: global\n"
        "models:\n  gpt-x:\n    name: gpt-x\n    base_url: https://example.test\n"
        "    api_key: sk-global\n",
    )
    _write(paths.workspace / "config.local.yaml", "default_model: project-model\n")

    app = ConfigLoader.load_layers(paths)

    assert app.default_model == "project-model"
    assert app.models["gpt-x"].api_key == "sk-global", (
        "the key is a fact about the person, not about the repo"
    )


def test_a_project_file_may_name_the_config_and_never_see_the_global_one(home):
    """An explicit ``--path`` means exactly that file, as it did before."""
    paths = Paths.for_workspace(home / "project")
    _write(paths.user_config, "default_model: global\n")
    explicit = _write(paths.workspace / "custom.yaml", "default_model: explicit\n")

    assert ConfigLoader.load_layers(paths, path=explicit).default_model == "explicit"


def test_a_project_layer_may_not_add_mcp_servers(home):
    """N-4: a repository cannot grant execution.

    This used to assert the opposite -- that servers from both layers are kept --
    which pinned ``_overlay``'s additive ``servers`` behaviour.  That behaviour is
    exactly what lets a project spawn an arbitrary process, so the contract is
    changed on purpose: the user's servers survive, the project's do not.
    """
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "mcp:\n  servers:\n    - name: global-one\n      transport: stdio\n"
        "      command: [echo]\n",
    )
    _write(
        paths.workspace / "config.local.yaml",
        "mcp:\n  servers:\n    - name: project-one\n      transport: stdio\n"
        "      command: [echo]\n",
    )

    names = [s.name for s in ConfigLoader.load_layers(paths).mcp.servers]

    assert names == ["global-one"]


def test_a_malformed_global_file_does_not_stop_the_project_from_loading(home):
    """A broken personal file must not make every project unstartable."""
    paths = Paths.for_workspace(home / "project")
    _write(paths.user_config, "default_model: [unclosed\n")
    _write(paths.workspace / "config.local.yaml", "default_model: project\n")

    assert ConfigLoader.load_layers(paths).default_model == "project"


def test_no_files_anywhere_means_defaults_not_a_crash(home):
    paths = Paths.for_workspace(home / "project")

    assert ConfigLoader.load_layers(paths).default_model == ""


def test_the_layer_it_came_from_is_reported(home):
    """A model that resolves differently in two projects needs to know which
    file answered, and a silent merge cannot answer that."""
    paths = Paths.for_workspace(home / "project")
    _write(paths.user_config, "default_model: global\n")

    report = ConfigLoader.load_layers_report(paths)

    assert report.config.default_model == "global"
    assert report.sources["default_model"] == str(paths.user_config)


def test_the_nearer_layer_is_the_one_named(home):
    paths = Paths.for_workspace(home / "project")
    _write(paths.user_config, "default_model: global\n")
    project = _write(paths.workspace / "config.local.yaml", "default_model: project\n")

    report = ConfigLoader.load_layers_report(paths)

    assert report.sources["default_model"] == str(project)


# ---- N-4: a project layer may not grant execution -------------------------
#
# ``config.example.yaml`` ships in the repository, so a project's config is not
# trusted to say what the harness may run.  The user layer still can, and
# ``AI_CODING_TRUST_PROJECT=1`` is the operator's explicit opt-in (a repository
# cannot set an environment variable for the agent process).


def _project_file(paths, text):
    return _write(paths.workspace / "config.local.yaml", text)


def test_a_project_hook_does_not_run(home):
    """The RCE this milestone exists for (review P0 of the plan)."""
    paths = Paths.for_workspace(home / "project")
    _project_file(
        paths,
        "hooks:\n  enabled: true\n"
        "  events:\n    - point: turn.start\n      command: ['echo', 'PWNED']\n",
    )

    app = ConfigLoader.load_layers(paths)

    assert app.hooks.enabled is False
    assert list(app.hooks.events) == []


def test_a_project_mcp_server_does_not_get_spawned(home):
    paths = Paths.for_workspace(home / "project")
    _project_file(
        paths,
        "mcp:\n  enabled: true\n  servers:\n    - name: evil\n"
        "      transport: stdio\n      command: ['echo']\n",
    )

    app = ConfigLoader.load_layers(paths)

    assert app.mcp.enabled is False
    assert list(app.mcp.servers) == []


def test_a_camel_case_variant_does_not_bypass_the_filter(home):
    """``_to_snake`` makes ``Hooks``/``MCP`` into ``hooks``/``mcp``.

    Filtering the raw document would leave a one-word bypass; these pin that the
    filter runs after normalization.
    """
    paths = Paths.for_workspace(home / "project")
    for key in ("Hooks", "HOOKS", "MCP", "Mcp"):
        _project_file(
            paths,
            f"{key}:\n  enabled: true\n"
            "  events:\n    - point: turn.start\n      command: ['echo', 'PWNED']\n"
            "  servers:\n    - name: evil\n      transport: stdio\n"
            "      command: ['echo']\n",
        )
        app = ConfigLoader.load_layers(paths)
        assert app.hooks.enabled is False, f"{key} bypassed the filter"
        assert app.mcp.enabled is False, f"{key} bypassed the filter"


def test_the_user_layers_own_hooks_survive(home):
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "hooks:\n  enabled: true\n"
        "  events:\n    - point: turn.start\n      command: ['echo', 'USER']\n",
    )
    _project_file(paths, "hooks:\n  enabled: true\n")

    app = ConfigLoader.load_layers(paths)

    assert app.hooks.enabled is True
    assert [list(e.command) for e in app.hooks.events] == [["echo", "USER"]]


def test_the_operators_opt_in_restores_the_project_layer(home, monkeypatch):
    monkeypatch.setenv("AI_CODING_TRUST_PROJECT", "1")
    paths = Paths.for_workspace(home / "project")
    _project_file(
        paths,
        "hooks:\n  enabled: true\n"
        "  events:\n    - point: turn.start\n      command: ['echo', 'PWNED']\n",
    )

    app = ConfigLoader.load_layers(paths)

    assert app.hooks.enabled is True
    assert [list(e.command) for e in app.hooks.events] == [["echo", "PWNED"]]


def test_only_the_execution_keys_are_dropped(home):
    paths = Paths.for_workspace(home / "project")
    _project_file(
        paths,
        "default_model: project-model\n"
        "mcp:\n  enabled: true\n  servers:\n    - name: evil\n"
        "      transport: stdio\n      command: ['echo']\n",
    )

    app = ConfigLoader.load_layers(paths)

    assert app.default_model == "project-model", "behaviour keys must keep working"
    assert app.mcp.enabled is False


def test_the_sidecar_is_not_read_from_the_project_directory(home):
    """``_mcp_doc`` resolves ``mcp.local.yaml`` next to ``chosen``.

    If ``chosen`` advanced to the project layer, a repository could drop a
    sidecar in its own directory and have it merged in (review P1-2).
    """
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "mcp:\n  enabled: true\n  servers:\n    - name: my-srv\n"
        "      transport: stdio\n      command: ['echo']\n",
    )
    _project_file(paths, "default_model: project-model\n")
    _write(
        paths.workspace / "mcp.local.yaml",
        "- name: evil-sidecar\n  transport: stdio\n  command: ['echo']\n",
    )

    names = [s.name for s in ConfigLoader.load_layers(paths).mcp.servers]

    assert names == ["my-srv"]


def test_the_sidecar_is_read_when_the_project_is_trusted(home, monkeypatch):
    monkeypatch.setenv("AI_CODING_TRUST_PROJECT", "1")
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "mcp:\n  enabled: true\n  servers:\n    - name: my-srv\n"
        "      transport: stdio\n      command: ['echo']\n",
    )
    _project_file(paths, "default_model: project-model\n")
    _write(
        paths.workspace / "mcp.local.yaml",
        "- name: trusted-sidecar\n  transport: stdio\n  command: ['echo']\n",
    )

    names = [s.name for s in ConfigLoader.load_layers(paths).mcp.servers]

    assert set(names) == {"my-srv", "trusted-sidecar"}


def test_a_project_key_does_not_advance_the_sidecar_anchor(home):
    """A behaviour key from the project must not drag ``chosen`` to it.

    Otherwise the sidecar bypass comes back through that seam.
    """
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "mcp:\n  enabled: true\n  servers:\n    - name: my-srv\n"
        "      transport: stdio\n      command: ['echo']\n",
    )
    _write(
        paths.workspace / "mcp.local.yaml",
        "- name: evil-sidecar\n  transport: stdio\n  command: ['echo']\n",
    )
    _project_file(paths, "default_model: project-model\n")

    app = ConfigLoader.load_layers(paths)

    assert app.default_model == "project-model"
    assert [s.name for s in app.mcp.servers] == ["my-srv"]


def test_an_explicit_path_is_not_filtered(home):
    """``--path`` is the operator's own file; it is not a "project layer"."""
    paths = Paths.for_workspace(home / "project")
    explicit = _write(
        paths.workspace / "custom.yaml",
        "hooks:\n  enabled: true\n"
        "  events:\n    - point: turn.start\n      command: ['echo', 'EXPLICIT']\n",
    )

    app = ConfigLoader.load_layers(paths, path=explicit)

    assert app.hooks.enabled is True
    assert [list(e.command) for e in app.hooks.events] == [["echo", "EXPLICIT"]]


def test_the_source_is_reported_as_the_user_layer(home):
    """After the drop, the surviving value really is the user layer's."""
    paths = Paths.for_workspace(home / "project")
    _write(
        paths.user_config,
        "default_model: global\n"
        "hooks:\n  enabled: true\n"
        "  events:\n    - point: turn.start\n      command: ['echo', 'USER']\n",
    )
    _project_file(paths, "hooks:\n  enabled: true\n")

    report = ConfigLoader.load_layers_report(paths)

    assert report.sources["hooks"] == str(paths.user_config)
    assert [list(e.command) for e in report.config.hooks.events] == [["echo", "USER"]]


def test_dropping_is_not_silent(home, caplog):
    paths = Paths.for_workspace(home / "project")
    _project_file(paths, "hooks:\n  enabled: true\n")

    with caplog.at_level("WARNING"):
        ConfigLoader.load_layers(paths)

    assert any("AI_CODING_TRUST_PROJECT" in r.message for r in caplog.records)


def test_no_warning_when_the_project_sets_nothing_executable(home, caplog):
    paths = Paths.for_workspace(home / "project")
    _project_file(paths, "default_model: project-model\n")

    with caplog.at_level("WARNING"):
        ConfigLoader.load_layers(paths)

    assert not [r for r in caplog.records if "AI_CODING_TRUST_PROJECT" in r.message]
