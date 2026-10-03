"""TDD (M2-4): skill tool — read SKILL.md files from a directory."""

import pytest

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


def test_skill_list(skill_dir):
    tool = SkillTool(skill_dir)
    names = tool.list_skills()
    assert "refactor" in names
    assert "testing" in names


@pytest.mark.asyncio
async def test_skill_read_content(skill_dir):
    tool = SkillTool(skill_dir)
    r = await tool.execute({"action": "read", "name": "refactor"})
    assert r.success is True
    assert "Refactor Skill" in r.output


@pytest.mark.asyncio
async def test_skill_list_action(skill_dir):
    tool = SkillTool(skill_dir)
    r = await tool.execute({"action": "list"})
    assert r.success is True
    assert "refactor" in r.output
    assert "testing" in r.output


@pytest.mark.asyncio
async def test_skill_read_missing(skill_dir):
    tool = SkillTool(skill_dir)
    r = await tool.execute({"action": "read", "name": "nope"})
    assert r.success is False
    assert "not found" in r.output.lower()


@pytest.mark.asyncio
async def test_skill_unknown_action(skill_dir):
    tool = SkillTool(skill_dir)
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
    tool = SkillTool(skill_dir)

    r = await tool.execute({"action": "list"})

    assert r.success is True
    plain = [line for line in r.output.splitlines() if line.strip().endswith("refactor")]
    assert plain, "a bare v1 skill keeps its old line"
    assert any("(v3)" in line for line in r.output.splitlines())
    assert any("[archived]" in line for line in r.output.splitlines())


@pytest.mark.asyncio
async def test_r11c_read_carries_version_and_status_in_data(tmp_path):
    _write_raw(tmp_path, "deploy", "description: ship\nversion: 3\nstatus: archived")
    tool = SkillTool(tmp_path)

    r = await tool.execute({"action": "read", "name": "deploy"})

    assert r.success is True
    assert r.data == {"name": "deploy", "version": "3", "status": "archived"}
    # One marker for both actions: read and list cannot disagree about a skill.
    assert r.output.startswith("# Skill: deploy (v3) [archived]")
