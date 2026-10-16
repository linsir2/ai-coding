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


def test_mcp_servers_from_both_layers_are_kept(home):
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

    assert set(names) == {"global-one", "project-one"}


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
