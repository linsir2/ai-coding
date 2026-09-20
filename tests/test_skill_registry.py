"""TDD (M3-3): SkillRegistry — scan, catalog, load, frontmatter parsing."""

from ai_coding.skills.registry import SkillRegistry


def _write_skill(root, name, description="A sample skill"):
    (root / name).mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\ndescription: {description}\n---\nHow to use {name}."
    (root / name / "SKILL.md").write_text(body, encoding="utf-8")


def test_scan_names(tmp_path):
    _write_skill(tmp_path, "bash")
    _write_skill(tmp_path, "read")
    (tmp_path / "notes").mkdir()  # not a skill (no SKILL.md)
    reg = SkillRegistry(tmp_path)
    assert reg.names() == ["bash", "read"]


def test_empty_dir_no_catalog(tmp_path):
    reg = SkillRegistry(tmp_path)
    assert reg.is_empty is True
    assert reg.catalog_text() == ""
    assert reg.names() == []


def test_catalog_text(tmp_path):
    _write_skill(tmp_path, "bash", "run shell commands")
    _write_skill(tmp_path, "read", "read files")
    reg = SkillRegistry(tmp_path)
    parts = reg.catalog_text().splitlines()
    assert "- bash: run shell commands" in parts
    assert "- read: read files" in parts


def test_load_frontmatter_description(tmp_path):
    _write_skill(tmp_path, "bash", "run commands in a shell")
    from ai_coding.skills.registry import _parse_description

    text = SkillRegistry(tmp_path).load("bash")
    assert text is not None
    assert text.startswith("---")
    assert _parse_description(text) == "run commands in a shell"


def test_load_missing_none(tmp_path):
    reg = SkillRegistry(tmp_path)
    assert reg.load("nope") is None
