"""TDD (M3-3): PromptAssembler — section ordering and conditional joins."""

from ai_coding.ai.prompt import PromptAssembler


def _assembler(plan_mode=False):
    return PromptAssembler("you are a coding assistant.", plan_mode=plan_mode)


def test_base_only():
    out = _assembler().build()
    assert "you are a coding assistant." in out
    assert "<system-reminder>" not in out
    assert "<skills>" not in out


def test_appends_project_and_skills_sections():
    out = _assembler().build(
        project_instructions="always use tabs",
        skills_catalog="- bash: run a shell\n- read: read a file",
    )
    assert "<system-reminder>Project instructions:" in out
    assert "always use tabs" in out
    assert "<skills>Available skills:" in out
    assert "- bash: run a shell" in out
    # section order: base(skill block) after project block
    assert out.index("<system-reminder>") < out.index("<skills>")


def test_plan_mode_segment():
    out = _assembler(plan_mode=True).build()
    assert "<plan-mode>" in out
    plain = _assembler(plan_mode=False).build()
    assert "<plan-mode>" not in plain


def test_memory_catalog():
    out = _assembler().build(memory_catalog="remember: paths matter")
    assert "<memory>Relevant prior notes:" in out
    assert "remember: paths matter" in out


def test_blank_optional_sections_omitted():
    out = _assembler().build(project_instructions="", skills_catalog="", memory_catalog="")
    assert "<system-reminder>" not in out
    assert "<skills>" not in out
    assert "<memory>" not in out
