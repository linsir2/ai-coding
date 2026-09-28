"""R10: bare ``ai-coding`` must find a config without being told where it is."""

from pathlib import Path

from ai_coding.config.loader import ConfigLoader


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_no_config_anywhere_returns_none(tmp_path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert ConfigLoader.discover(deep) is None


def test_finds_config_in_the_start_directory(tmp_path):
    _write(tmp_path / "config.local.yaml", "default_model: x")
    assert ConfigLoader.discover(tmp_path) == tmp_path / "config.local.yaml"


def test_climbs_to_a_parent_directory(tmp_path):
    _write(tmp_path / "config.local.yaml", "default_model: x")
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert ConfigLoader.discover(deep) == tmp_path / "config.local.yaml"


def test_local_wins_over_example(tmp_path):
    _write(tmp_path / "config.local.yaml", "default_model: real")
    _write(tmp_path / "config.example.yaml", "default_model: example")
    assert ConfigLoader.discover(tmp_path) == tmp_path / "config.local.yaml"


def test_config_yaml_wins_over_example(tmp_path):
    _write(tmp_path / "config.yaml", "default_model: plain")
    _write(tmp_path / "config.example.yaml", "default_model: example")
    assert ConfigLoader.discover(tmp_path) == tmp_path / "config.yaml"


def test_a_starting_file_uses_its_own_directory(tmp_path):
    target = _write(tmp_path / "config.local.yaml", "default_model: x")
    assert ConfigLoader.discover(target) == target


def test_load_app_without_a_path_uses_the_discovered_file(tmp_path):
    _write(
        tmp_path / "config.local.yaml",
        "models:\n  m:\n    name: m\ndefault_model: m\n",
    )
    cfg = ConfigLoader.load_app_from(tmp_path)
    assert cfg.default_model == "m"


def test_a_workspace_with_no_config_still_loads_defaults(tmp_path):
    cfg = ConfigLoader.load_app_from(tmp_path)
    assert cfg.default_model == ""
    assert cfg.models == {}
