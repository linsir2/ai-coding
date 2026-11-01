"""Codex-parity plan flow on the real loop (scripted model, no provider).

These build the actual ``AgentLoop`` -- request assembly, tool dispatch,
``update_plan``, the turn policy and persistence -- and script only the model's
steps.  The scenarios are the document-level dry runs T1/T3/T6:

* a plan turn emits ``<proposed_plan>``, the card answers "yes", and the
  implementation turn's prompt is byte-for-byte ``"Implement the plan."``;
* a text-only stop during implementation ends the turn with items still open
  (Codex has no continuation, no task gate);
* the whole flow carries no ``[plan-run]``/``[plan-continue]`` message and no
  ``<task-state>``/``<plan-mode>`` in the implementation requests.
"""

import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.app.commands import handle_input
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.security.user_prompt import UserAnswer
from ai_coding.service.session_service import SessionService
from ai_coding.tools.bash_tool import BashTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.update_plan_tool import UpdatePlanTool

PROPOSED = (
    "先看一下现有结构。\n"
    "<proposed_plan>\n"
    "1. 加一个健康检查端点\n"
    "2. 补一条测试\n"
    "</proposed_plan>"
)


class _Scripted(ChatEngine):
    def __init__(self, steps):
        self.steps = list(steps)
        self.seen: list[list] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.seen.append(list(messages))
        if not self.steps:
            raise AssertionError("model called more times than scripted")
        item = self.steps.pop(0)
        if on_delta is not None and item.text:
            on_delta(item.text)
        return item

    async def aclose(self):
        return None


class _Console:
    def __init__(self):
        self.lines: list[str] = []

    def info(self, text):
        self.lines.append(str(text))

    def error(self, text):
        self.lines.append(str(text))

    def rule(self, _text=""):
        return None

    def delta(self, _text):
        return None


class _Prompt:
    def __init__(self, answer: str):
        self.answer = answer
        self.asked: list[dict] = []

    async def ask(self, question, options=(), *, detail="", intent="", data=None):
        self.asked.append(
            {"question": question, "options": tuple(options), "detail": detail,
             "intent": intent, "data": data or {}}
        )
        return UserAnswer(answered=True, text=self.answer)


def _text(value):
    return StepResult(text=value, finish_reason="stop")


def _call(call_id, name, **params):
    return StepResult(
        text=None,
        tool_calls=[ToolCallRef(id=call_id, name=name, arguments=json.dumps(params))],
        finish_reason="tool_calls",
    )


def _loop(engine, tmp_path):
    registry = ToolRegistry()
    registry.register(UpdatePlanTool())
    registry.register(BashTool())
    return AgentLoop(
        engine,
        SessionService(tmp_path / "sessions"),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(),
        workspace=str(tmp_path / "ws"),
    )


@pytest.fixture(autouse=True)
def _workspace(tmp_path):
    (tmp_path / "ws").mkdir()
    return tmp_path


def _contents(requests):
    return "\n".join(m.content or "" for request in requests for m in request)


@pytest.mark.asyncio
async def test_propose_then_implement_in_two_turns(tmp_path):
    engine = _Scripted(
        [
            _text(PROPOSED),
            _call(
                "c1",
                "update_plan",
                plan=[
                    {"step": "加一个健康检查端点", "status": "in_progress"},
                    {"step": "补一条测试", "status": "pending"},
                ],
            ),
            _call("c2", "bash", command="touch endpoint.txt"),
            _call(
                "c3",
                "update_plan",
                plan=[
                    {"step": "加一个健康检查端点", "status": "completed"},
                    {"step": "补一条测试", "status": "in_progress"},
                ],
            ),
            _text("第一步完成。"),
        ]
    )
    loop = _loop(engine, tmp_path)
    session = loop.sessions.create()
    session.agent = "build"
    console = _Console()
    prompt = _Prompt("Yes, implement this plan")

    await handle_input(
        loop, "/plan 加健康检查", session, loop.sessions, console,
        user_prompt=prompt,
    )

    # One planning turn + one implementation turn; the execution prompt is the
    # literal Codex line and the plan body went to the card.
    assert session.turn_count == 2
    assert engine.steps == []
    assert prompt.asked[0]["intent"] == "plan_implement"
    assert "加一个健康检查端点" in prompt.asked[0]["detail"]
    implementation = [m for m in session.messages if m.content == "Implement the plan."]
    assert len(implementation) == 1
    assert session.mode == "normal" and session.readonly is False

    # The plan table is the model's, written by update_plan (2 items, no verify).
    assert [item.content for item in session.task.items] == [
        "加一个健康检查端点", "补一条测试"
    ]
    assert [item.status for item in session.task.items] == ["completed", "in_progress"]
    assert all(not item.verify for item in session.task.items)

    # Codex parity: no injected continuation text, anywhere.
    transcript = "\n".join(m.content or "" for m in session.messages)
    assert "[plan-run]" not in transcript
    assert "[plan-continue]" not in transcript

    # The planning request carries <plan-mode> and no <task-state>; the
    # implementation requests carry neither.
    planning_request = _contents(engine.seen[:1])
    assert "<plan-mode>" in planning_request
    assert "<task-state>" not in planning_request
    for request in engine.seen[1:]:
        text = _contents([request])
        assert "<task-state>" not in text
        assert "<plan-mode>" not in text


@pytest.mark.asyncio
async def test_text_stop_leaves_items_open_without_continuing(tmp_path):
    engine = _Scripted(
        [
            _call(
                "c1",
                "update_plan",
                plan=[{"step": "第一步", "status": "completed"},
                      {"step": "第二步", "status": "pending"}],
            ),
            _text("我先停一下。"),
        ]
    )
    loop = _loop(engine, tmp_path)
    session = loop.sessions.create()
    session.agent = "build"
    session.plan_implementing = True
    session.plan_verify_items = False

    await loop.process_input(session, "Implement the plan.", synthetic=True)
    session.plan_implementing = False

    assert session.turn_count == 1
    assert len(engine.seen) == 2  # one tool batch, then the text-only stop
    assert [item.status for item in session.task.items] == ["completed", "pending"]
    assert "Implement the plan." in [m.content for m in session.messages]


@pytest.mark.asyncio
async def test_plan_run_without_a_plan_still_submits_the_literal(tmp_path):
    engine = _Scripted([_text("没有计划可执行，请先 /plan 描述需求。")])
    loop = _loop(engine, tmp_path)
    session = loop.sessions.create()
    session.agent = "build"
    console = _Console()

    await handle_input(loop, "/plan run", session, loop.sessions, console,
                       user_prompt=_Prompt("No, stay in Plan mode"))

    assert session.turn_count == 1
    assert len(engine.seen) == 1
    assert "Implement the plan." in [m.content for m in session.messages]
    assert session.mode == "normal" and session.readonly is False
