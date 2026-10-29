"""R21 §10: one malformed ``tool_calls[].arguments`` must not poison a session.

Measured on a real long run: ``max_tokens`` truncated a ``write`` call mid-JSON;
the assistant message kept the half-written string, and **every** later request
was rejected with

    400 InternalError.Algo.InvalidParameter:
    The "function.arguments" parameter of the code model must be in JSON format.

Retrying the same body cannot help, so the turn died and every following round
re-sent the same history.  Four layers are pinned here:

1. the wire mapping never emits unparseable arguments (repairs, always);
2. the dispatcher refuses such a call instead of running it with ``{}``;
3. a step truncated by ``finish_reason == "length"`` refuses its calls, so a
   half-written file is never executed;
4. the engine can rebuild a request and retry once when a provider rejects it
   for that reason.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from openai import APIStatusError

from ai_coding.ai.mapping import to_chat_message
from ai_coding.ai.openai_engine import FailureKind, classify_failure
from ai_coding.domain.message import (
    ChatMessage,
    ToolCallRef,
    arguments_are_valid,
    normalize_arguments,
)


def _assistant(*refs: ToolCallRef) -> ChatMessage:
    return ChatMessage(role="assistant", content=None, timestamp="t", tool_calls=list(refs))


# ---- 1. the wire is always legal -------------------------------------------


def test_truncated_arguments_are_repaired_on_the_wire():
    """The string that killed the run: an unterminated JSON object."""
    broken = '{"path": "/ws/src/KanbanBoard.tsx", "content": "import { useEffect'
    assert not arguments_are_valid(broken)
    assert normalize_arguments(broken) == "{}"

    mapped = to_chat_message(_assistant(ToolCallRef(id="c1", name="write", arguments=broken)))
    wire_args = mapped["tool_calls"][0]["function"]["arguments"]
    json.loads(wire_args)  # must not raise: this is what the provider checks


def test_a_valid_arguments_string_survives_untouched():
    raw = '{"path": "a.py"}'
    assert arguments_are_valid(raw)
    assert normalize_arguments(raw) == raw
    mapped = to_chat_message(_assistant(ToolCallRef(id="c1", name="read", arguments=raw)))
    assert mapped["tool_calls"][0]["function"]["arguments"] == raw


def test_non_object_json_is_not_a_valid_call():
    """``[1,2]`` parses but is not a params mapping; sending it is a 400 too."""
    assert not arguments_are_valid("[1, 2]")
    assert normalize_arguments("[1, 2]") == "{}"
    assert not arguments_are_valid("")
    assert normalize_arguments("") == "{}"


# ---- 2/3. the dispatcher refuses instead of executing ----------------------

from ai_coding.core.dispatch import ToolDispatcher  # noqa: E402
from ai_coding.domain.message import ToolResult  # noqa: E402
from ai_coding.domain.task import TaskState  # noqa: E402
from ai_coding.security.approval import ApprovalResult  # noqa: E402
from ai_coding.security.permission_gate import PermissionGate  # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402


class _Recorder(BaseTool):
    """A ``write``-shaped tool that only records what reached it."""

    parallel_safe = False
    mutates_world = True

    def __init__(self) -> None:
        self.seen: list[dict] = []

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return "fake write"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.seen.append(dict(params))
        return ToolResult(success=True, output="ok")


class _Yes:
    """``write`` is WARN-tier; the real gate is not what these tests pin."""

    is_human = False

    async def approve(self, name, params, reason):  # noqa: ANN001, ANN201
        return ApprovalResult.YES


def _dispatcher(tool: BaseTool) -> ToolDispatcher:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolDispatcher(registry, PermissionGate(), _Yes())


def _ctx() -> ToolContext:
    return ToolContext(task=TaskState(goal="g"), workspace="/tmp/ws", session_id="s")


@pytest.mark.asyncio
async def test_a_truncated_call_is_refused_and_never_runs():
    tool = _Recorder()
    broken = '{"path": "/ws/a.tsx", "content": "import { use'
    outcomes = await _dispatcher(tool).dispatch_batch(
        [ToolCallRef(id="c1", name="write", arguments=broken)], _ctx()
    )
    assert tool.seen == []                     # nothing was written
    assert outcomes[0].result.success is False
    assert outcomes[0].result.error == "invalid_arguments"
    assert outcomes[0].denied is True
    # The model must be told what to do, not just that it failed.
    assert "拆" in outcomes[0].result.output or "分段" in outcomes[0].result.output


@pytest.mark.asyncio
async def test_a_truncated_step_refuses_every_call_in_the_batch():
    """``finish_reason == "length"`` means the arguments may be silently cut
    even when they happen to parse; the whole batch is refused."""
    tool = _Recorder()
    outcomes = await _dispatcher(tool).dispatch_batch(
        [ToolCallRef(id="c1", name="write", arguments='{"path": "a.py"}')],
        _ctx(),
        truncated=True,
    )
    assert tool.seen == []
    assert outcomes[0].result.error == "truncated_arguments"
    assert outcomes[0].denied is True


@pytest.mark.asyncio
async def test_a_valid_call_still_runs_under_the_new_rule():
    tool = _Recorder()
    outcomes = await _dispatcher(tool).dispatch_batch(
        [ToolCallRef(id="c1", name="write", arguments='{"path": "a.py"}')], _ctx()
    )
    assert tool.seen == [{"path": "a.py"}]
    assert outcomes[0].result.success is True


# ---- 4. the provider's own 400 is recoverable ------------------------------


def _status_error(status: int, message: str, body: dict | None = None) -> APIStatusError:
    import httpx

    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request)
    error = APIStatusError(message, response=response, body=body)
    return error


def test_a_malformed_tool_arguments_400_has_its_own_verdict():
    """``FATAL`` made the loop retry the identical body four times and die."""
    exc = _status_error(
        400,
        "Error code: 400 - {'error': {'message': 'InternalError.Algo.InvalidParameter: "
        'The "function.arguments" parameter of the code model must be in JSON format.\'}}',
    )
    assert classify_failure(exc) is FailureKind.INVALID_ARGUMENTS
    # Another provider would reject the same body, so it is not "worth another
    # model" -- the fix is on our side (rebuild the request).
    from ai_coding.ai.openai_engine import WORTH_ANOTHER_MODEL

    assert FailureKind.INVALID_ARGUMENTS not in WORTH_ANOTHER_MODEL


def test_a_length_error_is_still_fatal():
    exc = _status_error(400, "bad request: unknown field 'foo'")
    assert classify_failure(exc) is FailureKind.FATAL


@pytest.mark.asyncio
async def test_a_step_cut_by_the_length_limit_runs_no_tool(tmp_path):
    """The wiring, not just the dispatcher: the loop must read ``finish_reason``.

    Measured: ``max_tokens`` cut a ``write`` mid-JSON.  Even when the tail
    happens to parse, the loop has no way to know the model was finished.
    """
    from ai_coding.domain.loop import StepResult
    from ai_coding.service.session_service import SessionService
    from tests.test_agent_loop_react import (
        ScriptedEngine,
        _loop,
        _ref,
        _registry,
        _Tool,
        _Yes,
    )

    tool = _Tool("write")
    engine = ScriptedEngine(
        [
            StepResult(
                text=None,
                tool_calls=[_ref("c1", "write", '{"path": "a.py"}')],
                finish_reason="length",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    sessions = SessionService(tmp_path / "sessions")
    loop = _loop(engine, sessions, registry=_registry(tool), approval=_Yes())
    session = sessions.create()

    assert await loop.process_input(session, "go") == "done"

    assert tool.calls == []
    assert any(
        "截断" in (m.content or "") for m in session.messages if m.role == "tool"
    )


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _Client:
    """First request fails with the measured 400; the rebuilt one succeeds."""

    def __init__(self, error: Exception) -> None:
        self.seen: list[dict] = []
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.seen.append(kwargs)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        return _Stream(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="ok", tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )
            ]
        )


@pytest.mark.asyncio
async def test_the_engine_rebuilds_the_request_and_retries_once():
    """The turn used to die on the first 400: the body was never rebuilt."""
    from ai_coding.ai.openai_engine import OpenAIChatEngine
    from ai_coding.domain.model_config import ModelConfig

    broken = '{"path": "/ws/src/KanbanBoard.tsx", "content": "import { useEffect'
    history = [
        ChatMessage(role="user", content="build it", timestamp="t"),
        _assistant(ToolCallRef(id="c1", name="write", arguments=broken)),
        ChatMessage(role="tool", content="ok", timestamp="t", tool_call_id="c1"),
    ]
    error = _status_error(
        400,
        'InternalError.Algo.InvalidParameter: The "function.arguments" parameter '
        "of the code model must be in JSON format.",
    )
    client = _Client(error)
    retries: list[dict] = []
    engine = OpenAIChatEngine(
        ModelConfig(name="m", base_url="https://example.invalid/v1", api_key="k"),
        client=client,
        on_retry=lambda **info: retries.append(info),
    )

    step = await engine.step(history, tools=[])

    assert step.text == "ok"
    assert len(client.seen) == 2
    # Every body that reached the provider carried a legal arguments string --
    # which is what the provider checks before it looks at anything else.
    for sent in client.seen:
        for message in sent["messages"]:
            for call in message.get("tool_calls") or ():
                assert json.loads(call["function"]["arguments"]) == {}
    assert retries and retries[0]["kind"] == FailureKind.INVALID_ARGUMENTS.value
