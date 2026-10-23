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

#: The two halves of the discriminator: the verbs that mean "there is
#: something to change" and the words that mean "there is only something to
#: conclude".  They decide which exit a request gets, so they have to be
#: stated *before* the exits themselves.  A block that opens with "every
#: request is answered with a plan" has already decided the question, and a
#: research request comes back with a plan that then hits the workspace gate
#: (measured: the production session's turn ended in a random retry loop).
_CHANGES = "implement, modify, add, fix, refactor"
_NO_CHANGE = "a survey, an analysis, a comparison"


def _plan_segment() -> str:
    """The plan-mode block, as the model sees it."""
    out = _assembler().build(mode="plan")
    return out[out.index("<plan-mode>"):]


def test_plan_mode_segment():
    out = _assembler().build(mode="plan")
    assert "<plan-mode>" in out
    assert "<plan-mode>" not in _assembler().build()


def test_normal_output_is_byte_identical():
    """18 §10.5: the mode parameter must not move a single byte of normal output."""
    expected = "you are a coding assistant.\n\n" + METHODS_SECTION
    assert _assembler().build() == expected
    assert _assembler().build(mode="normal") == expected


def test_the_plan_segment_is_the_last_section_of_a_plan_prompt():
    """The block still sits where it did: after the methods, and nothing else
    is added for the planning posture."""
    out = _assembler().build(mode="plan")
    head, _, segment = out.partition("<plan-mode>")

    assert head == "you are a coding assistant.\n\n" + METHODS_SECTION + "\n\n"
    assert segment.endswith("</plan-mode>")


def test_the_plan_segment_says_which_exit_before_it_names_the_exits():
    """R9: the discriminator comes first, the two exits after it.

    The words are free; the order is not.  What the model must learn is
    *which* exit the request wants before it learns what the exits are --
    otherwise the block's own framing decides the question for it.
    """
    segment = _plan_segment()

    assert "present_plan" in segment, "the plan exit"
    assert "finish_research" in segment, "the research exit"
    assert _CHANGES in segment, "the rule that asks for a plan"
    assert _NO_CHANGE in segment, "the rule that asks for a conclusion"
    assert "never with the finished work" in segment, (
        "the real contract: the answer is a plan or a conclusion, not the work"
    )
    assert segment.index(_CHANGES) < segment.index("present_plan")
    assert segment.index(_CHANGES) < segment.index("finish_research")
    assert segment.index(_NO_CHANGE) < segment.index("present_plan")
    assert segment.index(_NO_CHANGE) < segment.index("finish_research")


def test_the_planning_prompt_says_which_exit_before_it_names_the_exits():
    """R9, the other half: the planning prompt carries the same rule.

    The prompt is the last message of the round, so it is the nearest thing
    to the model's next step -- and its first sentence used to command a
    plan outright ("Create a detailed implementation plan for: ..."), which
    is what turned a research request into a plan.
    """
    from ai_coding.core.plan_runner import PLAN_PROMPT

    assert "present_plan" in PLAN_PROMPT, "the plan exit"
    assert "finish_research" in PLAN_PROMPT, "the research exit"
    assert _CHANGES in PLAN_PROMPT, "the rule that asks for a plan"
    assert _NO_CHANGE in PLAN_PROMPT, "the rule that asks for a conclusion"
    assert PLAN_PROMPT.index(_CHANGES) < PLAN_PROMPT.index("present_plan")
    assert PLAN_PROMPT.index(_CHANGES) < PLAN_PROMPT.index("finish_research")
    assert PLAN_PROMPT.index(_NO_CHANGE) < PLAN_PROMPT.index("present_plan")
    assert PLAN_PROMPT.index(_NO_CHANGE) < PLAN_PROMPT.index("finish_research")


def test_an_unknown_mode_reads_as_normal():
    """Fail-safe: only "plan" changes the prompt (persistence filters too)."""
    assert _assembler().build(mode="weird") == _assembler().build()


def test_methods_section_sits_before_the_plan_mode_segment():
    out = _assembler().build(
        project_instructions="p", skills_catalog="s", mode="plan"
    )
    assert out.index("<methods>") < out.index("<plan-mode>") < out.index("<skills>")


def test_the_plan_segment_names_no_tools():
    """M2 T3: the segment must not contradict the tool surface.

    It used to say "bash/write/edit are unavailable here".  That was true
    while the read-only posture was a structural veto -- but the posture is
    now rules, and the rules have two tiers (soft and strict).  A text
    that names tools is a text that can be wrong about which ones; the
    surface itself is the authority, and it already prunes.
    """
    segment = _plan_segment()

    # The three that the surface prunes in plan mode, as *tool names*.
    # The words appear inside prose ("Approval writes those items",
    # "do not write the analysis"), so a substring test would flag the
    # English verb; what matters is that the segment does not name a tool
    # the surface removed -- and the surface is what the model reads.
    for tool in ("bash/write/edit", "read/grep/glob"):
        assert tool not in segment, f"the segment still names {tool}"


def test_the_plan_segment_still_says_what_to_do():
    """Narrowing the segment must not empty it."""
    segment = _plan_segment()

    assert "present_plan" in segment
    assert "finish_research" in segment
# ---- C-2: the methods section follows the tools the request actually carries ---


def test_a_read_only_surface_drops_what_it_cannot_do():
    """The two reference harnesses both prune guidance by the tool surface
    (dsh: `ctx.tools.get(name, scope) === undefined ? '' : '...'`;
    opencode: `${OPENCODE_TOOL_GUIDANCE}` rendered from the request's own tool
    names). Ours has a sharper signal still -- under a read-only posture the
    surface itself is filtered -- so the prompt must not teach bash habits to a
    model that cannot call bash."""
    out = _assembler().build(tools=("read", "grep", "glob"))

    assert "not shell commands like cat" in out
    assert "exit status" not in out
    assert "Write replaces a whole file" not in out


def test_the_unpruned_text_still_carries_every_tool_row():
    """A regression guard for a reversal this table once had: `None` means "the
    caller does not know its surface", so it must render *everything* -- the
    first version rendered only the general rows and made the unpruned prompt
    shorter than a read-only one, which is backwards."""
    for marker in ("exit status", "not shell commands like cat", "not shell find"):
        assert marker in _assembler().build(), marker


def test_no_tool_list_means_no_pruning():
    """Sub-agents and defensive paths do not know their surface at build time;
    they must keep the full text rather than silently lose discipline."""
    assert _assembler().build() == "you are a coding assistant.\n\n" + METHODS_SECTION


def test_a_surface_of_nothing_still_keeps_the_general_discipline():
    """Guidance that is not about a tool cannot be pruned by a tool surface."""
    out = _assembler().build(tools=())

    assert "Read before write" in out
    assert "Verify the change" in out


def test_tool_guidance_never_repeats_a_tool_description():
    """dsh's ownership rule: per-tool semantics stay in the description, and a
    prompt section carries only cross-call habits. 'oldString' is what edit's
    schema description already says; repeating it here is the drift the rule
    exists to prevent."""
    assert "oldString" not in METHODS_SECTION


def test_the_methods_section_stays_inside_its_budget():
    """Neither harness measures this (dsh has no counter at all; opencode has no
    cap), but this repo has already paid for a 556-character schema advertising
    a tool that was not connected. The section is ~2x today; the test is the
    tripwire that stops it becoming an order of magnitude."""
    assert len(METHODS_SECTION) <= 900, len(METHODS_SECTION)


def test_a_subagent_surface_keeps_its_own_habits():
    """subAgent is only in the main session's registry; a child never spawns one,
    so the paragraph must not be in the child's prompt."""
    assert "do not sleep or poll" in _assembler().build(tools=("bash", "subAgent"))
    assert "do not sleep or poll" not in _assembler().build(tools=("bash",))


def test_plan_segment_sends_the_steps_through_present_plan():
    """P1 (trace ec89d5ee): the segment taught a ``todo_write`` workflow whose
    measured cost was one iteration per step plus an add/clear loop.  The
    steps now ride ``present_plan``'s own call, and the planning phase is told
    not to track itself with a todo list.

    M3 T8 removed ``todo_write`` from the planning surface entirely, so the
    segment must not name it: a prompt that teaches "don't use this tool" for
    a tool the request does not carry is a second, drifting source of truth.
    The behavioural half of the old assertion -- "do not build a side list" --
    is still pinned.
    """
    out = _assembler().build(mode="plan")
    assert "present_plan" in out
    assert "items" in out
    assert "todo items with todo_write" not in out  # the old teaching is gone
    assert "todo_write" not in out
    assert "do not track the planning with a todo list" in out
