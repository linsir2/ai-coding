"""TDD (M1b T1b): who answered, and who was in charge when.

Three separate facts, each with its own consumer:

* every assistant message records its agent — the transcript tags it;
* the session keeps a durable switch history — a replay draws the same
  "Switched agent" rows the live stream showed;
* the live event carries the same words — one phrasing in two transports.

The execution layer's bubbles are the exception that proves the rule: a plan
step's report is *work*, so it is attributed to build even while the session
is in plan mode.
"""

from __future__ import annotations

from typing import get_args

from ai_coding.ai.prompt import PromptAssembler
from ai_coding.core.events import AgentSwitched, LoopEvent
from ai_coding.domain.loop import StepResult
from ai_coding.domain.run import assistant_message
from ai_coding.domain.session import SessionData
from ai_coding.server.bridge import to_wire


def test_an_assistant_message_records_its_agent():
    message = assistant_message(StepResult(text="hi"), "t", agent="plan")
    assert message.agent == "plan"
    assert assistant_message(StepResult(text="hi"), "t").agent == ""


def test_the_agent_is_written_only_when_present():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.add_message(assistant_message(StepResult(text="hi"), "t", agent="plan"))
    assert session.to_dict()["messages"][0]["agent"] == "plan"

    plain = SessionData(
        session_id="s", title="", created_time="t", last_access_time="t"
    )
    plain.add_message(assistant_message(StepResult(text="hi"), "t"))
    assert "agent" not in plain.to_dict()["messages"][0]


def test_the_switch_event_is_part_of_the_loop_event_union():
    assert AgentSwitched in get_args(LoopEvent)


def test_the_wire_mapping_names_both_ends_and_the_reason():
    wire = to_wire(
        AgentSwitched(from_agent="build", to_agent="plan", reason="/plan", ts="t")
    )
    assert wire == {
        "type": "agent.switched",
        "data": {"from": "build", "to": "plan", "reason": "/plan"},
    }


def test_the_read_only_axis_is_absent_unless_the_producer_knows_it():
    """R21 §2: ``None`` means "not reported" and must not become a JSON null.

    An agent-only producer keeps the exact old payload -- the client reads a
    *present* boolean as the truth and an absent key as "keep what you have".
    """
    silent = to_wire(AgentSwitched(from_agent="build", to_agent="plan", ts="t"))
    assert "readonly" not in silent["data"]

    reported = to_wire(
        AgentSwitched(from_agent="plan", to_agent="build", readonly=False)
    )
    assert reported["data"]["readonly"] is False


def test_message_finished_carries_the_agent_only_when_attributed():
    from ai_coding.core.events import AssistantMessage

    plain = to_wire(AssistantMessage(text="done", reasoning="hmm"))
    assert plain == {
        "type": "message.finished",
        "data": {"text": "done", "reasoning": "hmm"},
    }
    tagged = to_wire(AssistantMessage(text="done", agent="plan"))
    assert tagged["data"]["agent"] == "plan"


def test_the_agent_instructions_sit_between_the_project_and_the_methods():
    out = PromptAssembler("BASE").build(
        project_instructions="PROJECT",
        agent_instructions="AGENT",
        skills_catalog="SKILLS",
    )
    assert out.index("BASE") < out.index("PROJECT") < out.index("AGENT") < out.index("SKILLS")


def test_an_empty_agent_body_adds_no_section():
    plain = PromptAssembler("BASE").build(project_instructions="P")
    spaced = PromptAssembler("BASE").build(
        project_instructions="P", agent_instructions="   "
    )
    assert plain == spaced
