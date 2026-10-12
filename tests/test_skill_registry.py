"""TDD (M3-3): SkillRegistry — discovery and parsing.

The catalog tests moved to ``tests/test_skill_catalog.py`` when prompt
formatting left this module: the registry answers "what is on disk", the catalog
answers "what fits in the budget", and a class that does both cannot be tested
for either.
"""

from ai_coding.skills.registry import SkillRegistry, parse_skill


def _write_skill(root, name, description="A sample skill"):
    (root / name).mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\ndescription: {description}\n---\nHow to use {name}."
    (root / name / "SKILL.md").write_text(body, encoding="utf-8")


def _write_raw(root, name, frontmatter):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")


def _parse(tmp_path, name, text):
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    target = tmp_path / name / "SKILL.md"
    target.write_text(text, encoding="utf-8")
    return parse_skill(target)


def test_scan_names(tmp_path):
    _write_skill(tmp_path, "bash")
    _write_skill(tmp_path, "read")
    (tmp_path / "notes").mkdir()  # not a skill (no SKILL.md)
    assert SkillRegistry(tmp_path).names() == ["bash", "read"]


def test_empty_dir_has_no_skills(tmp_path):
    reg = SkillRegistry(tmp_path)
    assert reg.is_empty is True
    assert reg.names() == []
    assert reg.all() == ()


def test_get_parses_the_description_and_the_body(tmp_path):
    _write_skill(tmp_path, "bash", "run commands in a shell")

    skill = SkillRegistry(tmp_path).get("bash")

    assert skill.description == "run commands in a shell"
    assert skill.body == "How to use bash.", "the body is what the model is asked to read"
    assert skill.is_healthy


def test_get_missing_is_none(tmp_path):
    assert SkillRegistry(tmp_path).get("nope") is None
    assert SkillRegistry(tmp_path).resolve("nope") is None


def test_resolve_accepts_the_declared_name_too(tmp_path):
    _write_raw(tmp_path, "on-disk", "name: portable\ndescription: d")

    reg = SkillRegistry(tmp_path)

    assert reg.resolve("on-disk").id == "on-disk"
    assert reg.resolve("portable").id == "on-disk"


# ---- R11c: version / status, read back from the same frontmatter ----------


def test_r11c_a_bare_skill_reports_the_defaults_not_a_guess(tmp_path):
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "SKILL.md").write_text("# Bare\nno frontmatter\n", encoding="utf-8")

    skill = SkillRegistry(tmp_path).get("bare")

    assert (skill.version, skill.status) == ("1", "active")
    assert skill.description == "" and skill.phase == "" and skill.alias is None


def test_r11c_version_and_status_are_read(tmp_path):
    _write_raw(
        tmp_path, "deploy", "name: deploy\ndescription: ship it\nversion: 3\nstatus: archived"
    )

    skill = SkillRegistry(tmp_path).get("deploy")

    assert skill.version == "3"
    assert skill.status == "archived"
    assert skill.description == "ship it"
    assert skill.is_archived


def test_r11c_an_unknown_status_does_not_invent_a_state(tmp_path):
    _write_raw(tmp_path, "weird", "description: d\nstatus: banana")

    assert SkillRegistry(tmp_path).get("weird").status == "active"


def test_triggers_are_read_as_the_four_dimensions(tmp_path):
    _write_raw(
        tmp_path,
        "routed",
        "name: routed\ndescription: d\ntriggers:\n  keywords: [构建, 发布]\n"
        "  tools: [bash]\n  failure_patterns:\n    - 构建失败",
    )

    triggers = SkillRegistry(tmp_path).get("routed").triggers

    assert triggers.keywords == ("构建", "发布")
    assert triggers.tools == ("bash",)
    assert triggers.failure_patterns == ("构建失败",)
    assert triggers


def test_a_scalar_trigger_is_read_as_a_one_element_list(tmp_path):
    """`keywords: 构建` is the same declaration as `keywords: [构建]`."""
    _write_raw(tmp_path, "scalar", "name: scalar\ndescription: d\ntriggers:\n  keywords: 构建")

    assert SkillRegistry(tmp_path).get("scalar").triggers.keywords == ("构建",)


def test_frontmatter_below_a_leading_paragraph_is_not_a_header(tmp_path):
    """The old regex was unanchored, so a `---` rule in the prose was read as a
    header and every field came back empty."""
    (tmp_path / "prose").mkdir()
    (tmp_path / "prose" / "SKILL.md").write_text(
        "Some prose.\n\n---\nname: sneaky\ndescription: not a header\n---\n\nmore\n",
        encoding="utf-8",
    )

    skill = SkillRegistry(tmp_path).get("prose")

    assert skill.description == "", "a rule in the body is body text"
    assert skill.alias is None
    assert "sneaky" in skill.body


# ---- one parse, one object -----------------------------------------------
#
# The registry used to have three of them: five regexes for scalars, a YAML pass
# for `triggers` with a regex fallback, and SkillTool re-reading each file
# through two of those regexes. Three sources that can disagree, and no way to
# tell which one is right. `Skill` is the single answer every consumer reads.


def test_the_registry_caches_by_mtime_and_notices_a_rewrite(tmp_path):
    from ai_coding.skills.registry import SkillRegistry

    _write_skill(tmp_path, "bash", "run shell commands")
    reg = SkillRegistry(tmp_path)

    first = reg.get("bash")
    assert first.description == "run shell commands"
    assert reg.get("bash") is first, "an unchanged file is parsed once, not per iteration"

    _write_skill(tmp_path, "bash", "run shell commands safely")
    assert reg.get("bash").description == "run shell commands safely"
