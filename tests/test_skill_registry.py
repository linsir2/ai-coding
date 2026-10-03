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


# ---- R11c: version / status, read back from the same frontmatter ----------


def _write_raw(root, name, frontmatter):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")


def test_r11c_meta_defaults_for_a_bare_skill(tmp_path):
    """No frontmatter is version 1 and active — the defaults, not a guess."""
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "SKILL.md").write_text("# Bare\nno frontmatter\n", encoding="utf-8")

    meta = SkillRegistry(tmp_path).meta("bare")

    assert meta == {"name": "bare", "version": "1", "status": "active", "description": None}


def test_r11c_meta_parses_version_and_status(tmp_path):
    _write_raw(
        tmp_path, "deploy", "name: deploy\ndescription: ship it\nversion: 3\nstatus: archived"
    )

    meta = SkillRegistry(tmp_path).meta("deploy")

    assert meta["version"] == "3"
    assert meta["status"] == "archived"
    assert meta["description"] == "ship it"


def test_r11c_meta_falls_back_to_active_on_an_unknown_status(tmp_path):
    """An unrecognised status must not invent a new state."""
    _write_raw(tmp_path, "weird", "description: d\nstatus: banana")

    assert SkillRegistry(tmp_path).meta("weird")["status"] == "active"


def test_r11c_meta_for_an_unknown_skill_claims_nothing(tmp_path):
    """A missing skill has no version to report; the placeholder says so."""
    meta = SkillRegistry(tmp_path).meta("nope")

    assert meta["name"] == "nope"
    assert meta["version"] == "1"
    assert meta["status"] == "active"
    assert meta["description"] is None
