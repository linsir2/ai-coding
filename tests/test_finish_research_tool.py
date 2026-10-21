"""N1: ``finish_research`` — the second exit of a plan-mode turn.

A planning turn that answers a research question has nothing to execute, so
it must not be forced into a plan (the measured malformation: the work is
done first, then a plan is back-filled with invented verify commands).  This
tool is that other exit: the model states its conclusion, the turn ends, and
nothing is armed, executed or reviewed.

The two exits are mutually exclusive -- the first one to succeed owns the
turn -- so the tests below pin both refusals as well as the success.
"""

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.task import TaskState
from ai_coding.tools.base import ToolContext, TurnConclusion
from ai_coding.tools.finish_research_tool import FinishResearchTool


def _ctx(
    *,
    mode: str = "plan",
    conclusion: TurnConclusion | None = None,
    messages: tuple[ChatMessage, ...] = (),
) -> ToolContext:
    return ToolContext(
        task=TaskState(goal="g"),
        workspace="/tmp/ws",
        session_id="sid",
        mode=mode,
        conclusion=conclusion,
        session_messages=messages,
    )


def _user(text: str, *, synthetic: bool = False) -> ChatMessage:
    return ChatMessage(role="user", content=text, timestamp="t", synthetic=synthetic)


@pytest.mark.asyncio
async def test_it_refuses_outside_plan_mode():
    """The exit belongs to the planning posture only.  A missing context fails
    closed the same way (the gate runs before anything else)."""
    tool = FinishResearchTool()

    r = await tool.execute({"summary": "结论"}, _ctx(mode="normal"))
    assert r.success is False and r.error == "not_in_plan"

    r2 = await tool.execute({"summary": "结论"}, None)
    assert r2.success is False and r2.error == "not_in_plan"


@pytest.mark.asyncio
async def test_it_refuses_an_empty_summary():
    tool = FinishResearchTool()

    r = await tool.execute({"summary": "   "}, _ctx())
    assert r.success is False and r.error == "missing_summary"


@pytest.mark.asyncio
async def test_it_closes_the_turn_with_the_conclusion_itself():
    """The summary is the deliverable: it is what the user reads, so it is the
    tool's output -- and nothing else happens.  No items, no arm, no card."""
    tool = FinishResearchTool()
    ctx = _ctx()

    r = await tool.execute({"summary": "记忆层是三层：working / episodic / semantic。"}, ctx)

    assert r.success is True
    assert r.output == "记忆层是三层：working / episodic / semantic。"
    assert r.data == {"research_finished": True}
    # Nothing was planned and nothing was armed: the runner must not wake up.
    assert ctx.task.items == []
    assert ctx.task.armed is False


@pytest.mark.asyncio
async def test_it_refuses_when_the_turn_already_concluded():
    """The exits are mutually exclusive: the first one to succeed owns the
    turn, so a research conclusion after a plan submission is refused."""
    tool = FinishResearchTool()
    conclusion = TurnConclusion(exit="present_plan")

    r = await tool.execute({"summary": "结论"}, _ctx(conclusion=conclusion))

    assert r.success is False and r.error == "already_concluded"
    assert "present_plan" in r.output


@pytest.mark.asyncio
async def test_it_refuses_while_a_plan_is_approved():
    """An approved plan owns the turn -- the runner is about to execute it, so
    a research conclusion would contradict the approval."""
    tool = FinishResearchTool()
    ctx = _ctx()
    ctx.task.armed = True
    ctx.task.armed_profile = "plan"

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is False and r.error == "plan_already_approved"


@pytest.mark.asyncio
async def test_it_refuses_while_a_goal_is_armed():
    """A running goal owns the task list; the research exit must not reach in
    and close a turn that is not its own."""
    tool = FinishResearchTool()
    ctx = _ctx()
    ctx.task.armed = True
    ctx.task.armed_profile = "goal"

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is False and r.error == "task_busy"


@pytest.mark.asyncio
async def test_a_successful_conclusion_marks_the_turn_concluded():
    """The holder is what the other exit reads: after this call, a plan
    submission in the same turn must be refused."""
    tool = FinishResearchTool()
    conclusion = TurnConclusion()
    ctx = _ctx(conclusion=conclusion)

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is True
    assert conclusion.exit == "finish_research"


# ---- the anti-abuse backstop (N1): the exit is for requests with no work ---


@pytest.mark.asyncio
async def test_it_refuses_a_request_that_asks_for_implementation():
    """The exit answers requests that need no implementation.  A request that
    asks for work must go through present_plan -- otherwise the plan comes
    back where a conclusion was wanted (the malformation this exit kills)."""
    tool = FinishResearchTool()
    ctx = _ctx(messages=(_user("给 src/mod1.py 加一个函数"),))

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is False and r.error == "implementation_request"
    assert "present_plan" in r.output


@pytest.mark.asyncio
async def test_it_accepts_a_research_request():
    """The other side: a request that only asks to find out is allowed, even
    when it mentions the implementation words to say what it does NOT want."""
    tool = FinishResearchTool()
    ctx = _ctx(
        messages=(
            _user(
                "调研这个仓库的会话是怎么落盘的：读代码找出答案并说明依据，"
                "不要提出实现改动，也不要写任何文件。"
            ),
        )
    )

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is True


@pytest.mark.asyncio
async def test_it_ignores_the_harness_planning_instruction():
    """The harness's own lines are not the user's request: the planning
    instruction is written in the user's voice (B3), and it is full of
    implementation words -- it must not decide what the exit is for."""
    tool = FinishResearchTool()
    ctx = _ctx(
        messages=(
            _user("看看记忆层怎么设计的"),
            _user("[plan] Create a detailed implementation plan for: x."),
        )
    )

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is True


@pytest.mark.parametrize(
    "utterance",
    [
        "给 src/mod1.py 加一个函数",
        "帮我实现一个登录接口",
        "implement a new endpoint",
        "add a function to mod1.py",
        "重构一下这个模块",
        "把登录改成异步的",
        "删除旧的配置",
    ],
)
@pytest.mark.asyncio
async def test_it_refuses_the_obvious_implementation_requests(utterance):
    """The blunt cases, in both languages: a request that asks for work must
    go through present_plan."""
    tool = FinishResearchTool()
    ctx = _ctx(messages=(_user(utterance),))

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is False and r.error == "implementation_request"


@pytest.mark.parametrize(
    "utterance",
    [
        "调研这个仓库的会话是怎么落盘的：读代码找出答案并说明依据",
        "不要改代码，只要调研记忆层的设计",
        "don't implement anything, just survey the code",
        "这个实现方式更好吗",
        "哪个方案更加简洁",
        "看看记忆层怎么设计的",
    ],
)
@pytest.mark.asyncio
async def test_it_accepts_the_research_requests(utterance):
    """The other side: a request that only asks to find out stays allowed --
    including the ones that mention the implementation words to say what they
    do NOT want, and the ones where the word is a noun."""
    tool = FinishResearchTool()
    ctx = _ctx(messages=(_user(utterance),))

    r = await tool.execute({"summary": "结论"}, ctx)

    assert r.success is True


# ---- one criterion, three places (the model must not learn two rules) -------


def _the_three_places_that_teach_the_exits() -> tuple[str, str, str]:
    """(the plan-mode prompt segment, PLAN_PROMPT, the tool's description)."""
    from ai_coding.ai.prompt import PromptAssembler
    from ai_coding.core.plan_runner import PLAN_PROMPT

    segment = PromptAssembler("you are a coding assistant.").build(mode="plan")
    return segment, PLAN_PROMPT, FinishResearchTool().description


def test_the_three_places_teach_the_same_criterion():
    """The plan-mode prompt, the planning prompt and the tool's own
    description must not each tell a different story: the same two exits,
    the same split (implementation vs no implementation), and the same change
    verbs that make a request an implementation request."""
    segment, plan_prompt, description = _the_three_places_that_teach_the_exits()
    for text in (segment, plan_prompt):
        assert "present_plan" in text
        assert "finish_research" in text
    # the description names the *other* exit (its own name is in the schema)
    assert "present_plan" in description
    for text in (segment, plan_prompt, description):
        # the research half: a request that needs no implementation
        assert "no implementation" in text
        # the implementation half: the same change verbs everywhere
        lowered = text.lower()
        for verb in ("implement", "modify", "add", "fix", "refactor"):
            assert verb in lowered, f"{verb} missing from {text!r}"
