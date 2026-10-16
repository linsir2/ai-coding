"""TDD (M3-3): PromptAssembler — section ordering and conditional joins."""

from ai_coding.ai.prompt import METHODS_SECTION, PromptAssembler


def _assembler() -> PromptAssembler:
    return PromptAssembler("you are a coding assistant.")


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


# ---- 18 §10.5 (P2): the posture rides build(mode=) ---------------------------

PLAN_SEGMENT = (
    "<plan-mode>You are in plan mode. Every request is answered with a plan, "
    "never with the finished work: explore only enough to plan, write the "
    "steps as todo items with todo_write (each with its verify command), then "
    "call present_plan with a concise markdown summary — approval starts "
    "execution automatically. Do not write the analysis, research or files as "
    "your final answer, and do not mark items completed — the execution phase "
    "does the reading and the work. bash/write/edit are unavailable here — "
    "explore with read/grep/glob instead.</plan-mode>"
)


def test_plan_mode_segment():
    out = _assembler().build(mode="plan")
    assert "<plan-mode>" in out
    assert "<plan-mode>" not in _assembler().build()


def test_normal_output_is_byte_identical():
    """18 §10.5: the mode parameter must not move a single byte of normal output."""
    expected = "you are a coding assistant.\n\n" + METHODS_SECTION
    assert _assembler().build() == expected
    assert _assembler().build(mode="normal") == expected


def test_plan_segment_is_verbatim_and_sits_after_methods():
    expected = (
        "you are a coding assistant.\n\n" + METHODS_SECTION + "\n\n" + PLAN_SEGMENT
    )
    assert _assembler().build(mode="plan") == expected


def test_an_unknown_mode_reads_as_normal():
    """Fail-safe: only "plan" changes the prompt (persistence filters too)."""
    assert _assembler().build(mode="weird") == _assembler().build()


def test_methods_section_sits_before_the_plan_mode_segment():
    out = _assembler().build(
        project_instructions="p", skills_catalog="s", mode="plan"
    )
    assert out.index("<methods>") < out.index("<plan-mode>") < out.index("<skills>")
