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
