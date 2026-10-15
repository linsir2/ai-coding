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


def test_blank_optional_sections_omitted():
    out = _assembler().build(project_instructions="", skills_catalog="")
    assert "<system-reminder>" not in out
    assert "<skills>" not in out
    # No <memory> section, and no parameter to ask for one: memory reaches the
    # model as a <memory-constraints> block on the request *tail*, never in the
    # system prompt — tests/test_agent_loop_memory.py pins that on the real loop,
    # where a decision note is injected and the prompt is unchanged.
    assert "<memory>" not in out


# ---- 13 §2.3 (A): the methods section ---------------------------------------


def test_methods_section_joins_between_project_and_skills():
    out = _assembler().build(
        project_instructions="always use tabs",
        skills_catalog="- bash: run a shell",
    )
    assert "<methods>" in out
    assert "Read before write" in out
    assert out.index("always use tabs") < out.index("<methods>")
    assert out.index("<methods>") < out.index("<skills>")


def test_methods_section_rides_even_a_base_only_prompt():
    assert "<methods>" in _assembler().build()


def test_methods_section_sits_before_the_plan_mode_segment():
    out = _assembler(plan_mode=True).build(
        project_instructions="p", skills_catalog="s"
    )
    assert out.index("<methods>") < out.index("<plan-mode>") < out.index("<skills>")
