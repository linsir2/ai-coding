"""TDD (r16/E-6): what a loaded skill looks like to the model.

One renderer for the tool's read and the @mention injection — two formatters
would drift the day one of them learned about files or status markers. The
format mirrors OpenCode's `Skill.prepare`/`toModelOutput`
(packages/core/src/skill.ts:36-65).
"""

from ai_coding.skills.registry import parse_skill
from ai_coding.skills.render import MAX_SKILL_FILES, render_skill_content, skill_files


def _skill(tmp_path, frontmatter="description: d\n", body="Do the thing.\n", files=()):
    root = tmp_path / "skills" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    (root / "SKILL.md").write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return parse_skill(root / "SKILL.md"), root


def test_the_content_is_wrapped_like_opencodes(tmp_path):
    skill, root = _skill(tmp_path)

    text = render_skill_content(skill)

    assert text.startswith('<skill_content name="demo">')
    assert "# Skill: demo\n\nDo the thing." in text
    assert f"Base directory for this skill: {root}" in text
    assert "Relative paths in this skill" in text
    assert text.endswith("</skill_files>\n</skill_content>")


def test_the_file_list_is_sorted_absolute_and_excludes_skill_md(tmp_path):
    skill, root = _skill(tmp_path, files=["scripts/run.sh", "reference/api.md"])

    files = skill_files(skill)

    assert files == sorted(files)
    assert str(root / "reference" / "api.md") in files
    assert str(root / "scripts" / "run.sh") in files
    assert all(not f.endswith("SKILL.md") for f in files)


def test_the_file_list_is_capped(tmp_path):
    skill, _ = _skill(tmp_path, files=[f"notes/f{i:02d}.md" for i in range(12)])

    assert len(skill_files(skill)) == MAX_SKILL_FILES


def test_a_skill_without_extra_files_still_declares_the_empty_list(tmp_path):
    skill, _ = _skill(tmp_path)

    text = render_skill_content(skill)

    assert "<skill_files>\n</skill_files>" in text


def test_the_status_marker_keeps_its_one_spelling(tmp_path):
    skill, _ = _skill(tmp_path, frontmatter="description: d\nversion: 3\nstatus: archived\n")

    text = render_skill_content(skill)

    assert "# Skill: demo (v3) [archived]" in text
