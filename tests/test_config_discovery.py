"""Config discovery: two bugs that made "change directory" mean "lose setup".

``discover()`` walked up from ``Path.cwd()`` and ignored ``-w`` entirely, so
``ai-coding -w /projects/B`` launched from ``~/scratch`` looked for B's config
under ``~/scratch`` and its parents, found nothing, and started on defaults with
``default_model: ""`` — i.e. it did not start. The same lookup nested the
directory loop outside the filename loop, so ``repo/sub/config.example.yaml``
shadowed ``repo/config.local.yaml``: the shipped example file won over the
operator's API key, purely because it sat one directory closer.
"""

from ai_coding.config.loader import ConfigLoader


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_discovery_is_anchored_on_the_workspace_not_the_process_cwd(tmp_path, monkeypatch):
    """`-w` is the workspace. The config of the workspace must be found even
    when the process was started somewhere else entirely."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    project = tmp_path / "project"
    _write(project / "config.local.yaml", "default_model: gpt-x\n")

    found = ConfigLoader.discover(start=project)

    assert found == project / "config.local.yaml"


def test_discovery_climbs_out_of_a_subdirectory_of_the_project(tmp_path):
    _write(tmp_path / "config.local.yaml", "default_model: gpt-x\n")
    deep = tmp_path / "src" / "nested"
    deep.mkdir(parents=True)

    assert ConfigLoader.discover(start=deep) == tmp_path / "config.local.yaml"


def test_a_real_config_is_never_shadowed_by_an_example_one_deeper_in(tmp_path):
    """Directory proximity must not outrank what the file *is*. The example file
    is documentation; it is the last resort, not the first hit."""
    _write(tmp_path / "config.local.yaml", "default_model: root-key\n")
    _write(tmp_path / "sub" / "config.example.yaml", "default_model: example\n")

    found = ConfigLoader.discover(start=tmp_path / "sub")

    assert found == tmp_path / "config.local.yaml"
    assert "root-key" in found.read_text(encoding="utf-8")


def test_the_example_file_is_still_found_when_it_is_all_there_is(tmp_path):
    """Otherwise a fresh clone, which ships only the example, would find
    nothing and start on empty defaults."""
    _write(tmp_path / "config.example.yaml", "default_model: example\n")

    assert ConfigLoader.discover(start=tmp_path) == tmp_path / "config.example.yaml"


def test_the_nearest_project_wins_over_a_further_one(tmp_path):
    _write(tmp_path / "config.local.yaml", "default_model: outer\n")
    inner = tmp_path / "inner"
    _write(inner / "config.local.yaml", "default_model: inner\n")

    assert ConfigLoader.discover(start=inner) == inner / "config.local.yaml"


def test_within_one_directory_the_more_specific_name_wins(tmp_path):
    """`config.local.yaml` holds the API key and is gitignored; the example
    file in the same directory must not win."""
    _write(tmp_path / "config.local.yaml", "default_model: keyed\n")
    _write(tmp_path / "config.example.yaml", "default_model: example\n")

    assert ConfigLoader.discover(start=tmp_path).name == "config.local.yaml"


def test_a_file_path_is_reduced_to_its_directory_before_the_search(tmp_path):
    """`--path` hands over a file; the same shape has to work when someone
    passes a path that exists, which the walk would otherwise treat as a
    directory and never match inside."""
    target = _write(tmp_path / "nested" / "config.local.yaml", "default_model: custom\n")

    found = ConfigLoader.discover(start=target)

    assert found == target


def test_nothing_found_is_none_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(ConfigLoader, "_ancestors", lambda base: [tmp_path])
    assert ConfigLoader.discover(start=tmp_path / "empty") is None


def test_a_home_directory_config_is_not_mistaken_for_a_project_one(tmp_path, monkeypatch):
    """`~` is an ancestor of every workspace, so walking up would always find a
    config there if one happened to exist. Project discovery stops at the user's
    home directory: a config for one repo must not leak into a sibling of
    another."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    _write(home / "config.local.yaml", "default_model: home\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

    found = ConfigLoader.discover(start=home / "proj")

    assert found != home / "config.local.yaml"
