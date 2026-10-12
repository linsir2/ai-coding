"""TDD (M2-4): skill tool — read SKILL.md files from a directory."""

import pytest

from ai_coding.skills.registry import SkillRegistry
from ai_coding.tools.skill_tool import SkillTool


@pytest.fixture
def skill_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    (d / "refactor").mkdir()
    (d / "refactor" / "SKILL.md").write_text("# Refactor Skill\nHow to refactor.\n")
    (d / "testing").mkdir()
    (d / "testing" / "SKILL.md").write_text("# Testing Skill\nHow to test.\n")
    return d


def _tool(skill_dir):
    """The tool is built from the registry now, never from a path — two roots
    were how the catalog and the tool came to disagree about what exists."""
    return SkillTool(SkillRegistry(skill_dir))


def test_skill_list(skill_dir):
    tool = _tool(skill_dir)
    names = tool.list_skills()
    assert "refactor" in names
    assert "testing" in names


@pytest.mark.asyncio
async def test_skill_read_content(skill_dir):
    tool = _tool(skill_dir)
    r = await tool.execute({"action": "read", "name": "refactor"})
    assert r.success is True
    assert "Refactor Skill" in r.output


@pytest.mark.asyncio
async def test_skill_list_action(skill_dir):
    tool = _tool(skill_dir)
    r = await tool.execute({"action": "list"})
    assert r.success is True
    assert "refactor" in r.output
    assert "testing" in r.output


@pytest.mark.asyncio
async def test_skill_read_missing(skill_dir):
    tool = _tool(skill_dir)
    r = await tool.execute({"action": "read", "name": "nope"})
    assert r.success is False
    assert "not found" in r.output.lower()


@pytest.mark.asyncio
async def test_skill_unknown_action(skill_dir):
    tool = _tool(skill_dir)
    r = await tool.execute({"action": "bogus"})
    assert r.success is False


# ---- R11c: the version / status the model is shown -----------------------


def _write_raw(root, name, frontmatter):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_r11c_list_marks_only_non_default_versions(skill_dir):
    """A v1 active skill must look exactly as it did before (no suffix)."""
    _write_raw(skill_dir, "deploy", "description: ship\nversion: 3\nstatus: archived")
    tool = _tool(skill_dir)

    r = await tool.execute({"action": "list"})

    assert r.success is True
    plain = [line for line in r.output.splitlines() if line.strip().endswith("refactor")]
    assert plain, "a bare v1 skill keeps its old line"
    assert any("(v3)" in line for line in r.output.splitlines())
    assert any("[archived]" in line for line in r.output.splitlines())


@pytest.mark.asyncio
async def test_r11c_read_carries_version_and_status_in_data(tmp_path):
    _write_raw(tmp_path, "deploy", "description: ship\nversion: 3\nstatus: archived")
    tool = SkillTool(SkillRegistry(tmp_path))

    r = await tool.execute({"action": "read", "name": "deploy"})

    assert r.success is True
    assert r.data == {"name": "deploy", "version": "3", "status": "archived"}
    # One marker for both actions: read and list cannot disagree about a skill.
    assert r.output.startswith("# Skill: deploy (v3) [archived]")


# ---- one directory walk, one parser ---------------------------------------
#
# The tool used to walk the skills directory itself and re-read each file
# through two of the registry's private regexes, so it and the catalog could
# disagree about what existed and what a skill said. Worse, the two were given
# different roots: `cli.py` always built the registry from `<workspace>/skills`
# while `tools/builder.py` registered the tool only when `--skills` was passed —
# so `ai-coding -w .` advertised skills in the system prompt that the model had
# no tool to load.


def test_the_tool_and_the_catalog_read_the_same_registry(tmp_path):
    from ai_coding.skills.catalog import build_catalog
    from ai_coding.skills.registry import SkillRegistry

    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "SKILL.md").write_text(
        "---\nname: one\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    registry = SkillRegistry(tmp_path)
    tool = SkillTool(registry)

    assert sorted(tool.list_skills()) == [skill.id for skill in registry.all()]
    assert build_catalog(registry.all()), "one skill on disk means one catalog line"


@pytest.mark.asyncio
async def test_reading_returns_the_body_not_the_frontmatter(skill_dir):
    """The old read handed back the whole file, so the model's context carried
    the frontmatter a second time after the catalog had already shown it."""
    from ai_coding.skills.registry import SkillRegistry

    tool = SkillTool(SkillRegistry(skill_dir))

    result = await tool.execute({"action": "read", "name": "refactor"})

    assert result.success is True
    assert "How to refactor." in result.output


@pytest.mark.asyncio
async def test_reading_by_the_declared_name_works_too(tmp_path):
    from ai_coding.skills.registry import SkillRegistry

    (tmp_path / "on-disk").mkdir()
    (tmp_path / "on-disk" / "SKILL.md").write_text(
        "---\nname: portable\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    tool = SkillTool(SkillRegistry(tmp_path))

    by_disk = await tool.execute({"action": "read", "name": "on-disk"})
    by_declared = await tool.execute({"action": "read", "name": "portable"})

    assert by_disk.success and by_declared.success
    assert by_declared.data["name"] == "on-disk", "one skill, one id"


@pytest.mark.asyncio
async def test_an_archived_skill_reads_but_says_so(skill_dir):
    """`status: archived` means "do not recommend", which is what Claude Code's
    `disable-model-invocation` does. Refusing the read would make a skill
    archived by mistake undebuggable, and would be a policy this tool has no
    business enforcing."""
    from ai_coding.skills.registry import SkillRegistry

    (skill_dir / "testing" / "SKILL.md").write_text(
        "---\nname: testing\ndescription: d\nstatus: archived\n---\nbody\n", encoding="utf-8"
    )
    tool = SkillTool(SkillRegistry(skill_dir))

    listed = await tool.execute({"action": "list"})
    read = await tool.execute({"action": "read", "name": "testing"})

    assert "[archived]" in listed.output, "listed, marked, not hidden"
    assert "[archived]" in read.output


@pytest.mark.asyncio
async def test_a_broken_skill_is_reported_not_swallowed(skill_dir):
    from ai_coding.skills.registry import SkillRegistry

    (skill_dir / "broken").mkdir()
    (skill_dir / "broken" / "SKILL.md").write_text(
        "---\nname: [unclosed\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    tool = SkillTool(SkillRegistry(skill_dir))

    result = await tool.execute({"action": "read", "name": "broken"})

    assert "无法解析" in result.output
