"""Acceptance (13 §1/§2.5/§2.7, 15 T4/T7/T8): the stage-1 input path over a real HTTP wire.

Not another unit test: every layer here is the production one -- the real
``OpenAIChatEngine`` with its real ``AsyncOpenAI`` client, ``cli._build_loop`` /
``_build_tool_registry`` assembly, the real guard chain, the real
``SessionService`` -- and only the model itself is a scripted HTTP server.  The
assertions read the *recorded request bodies*, so a drift anywhere between the
ladder and the wire (a dropped synthetic message, a ``tools`` array that should
not be there, a missing environment block, a retry that duplicated text) fails
here even when every unit stays green.

Claims under test:
  * ladder closure: 2 failed reads -> L1 nudge -> duplicate refusal -> L2 (armed
    refusal) -> L3 ask -> ask_user -> helped -> finish -- all visible in the
    request sequence, with the outcome persisted
  * a broken stream after output continues exactly once with the persisted
    ``[stream-interrupted]`` note (B)
  * a three-way-empty step retries once and the retry carries no injected
    message (H)
  * the closing call (iteration-budget ending only) offers no tools (G)

The turn-close chain (memory extractor, 13 §3 "closer" zero-change) shares the
same endpoint, so it shows up in the recordings too; it is asserted separately
instead of polluting the conversation-request checks.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ai_coding.ai.factory import build_engine_from_config
from ai_coding.cli import _build_loop, _build_tool_registry
from ai_coding.config.loader import ConfigLoader
from ai_coding.core.agent_loop import _CLOSING_PROMPT
from ai_coding.domain.message import ToolResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool, ToolContext

READ_MISSING = json.dumps({"path": "missing.txt"})


# ---------------------------------------------------------------- fake model


def _chunk(delta: dict[str, Any] | None = None, finish: str | None = None) -> dict:
    return {
        "id": "chunk",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _tool_call(name: str, arguments: str) -> list[dict]:
    return [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{name}_{abs(hash(arguments)) % 10000}",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ]
            },
            finish="tool_calls",
        )
    ]


def _text(text: str) -> list[dict]:
    return [_chunk({"content": text}), _chunk({}, finish="stop")]


def _respond(*payloads: dict):
    def step(handler: BaseHTTPRequestHandler) -> None:
        for payload in payloads:
            handler.wfile.write(_sse(payload))
        handler.wfile.write(b"data: [DONE]\n\n")

    return step


def _cut_after(*payloads: dict):
    """Deliver some output, then abort the TCP connection (reset, not EOF)."""

    def step(handler: BaseHTTPRequestHandler) -> None:
        for payload in payloads:
            handler.wfile.write(_sse(payload))
        handler.wfile.flush()
        handler.connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        handler.connection.close()

    return step


class _FakeModel:
    """A scripted OpenAI-compatible endpoint that records every request body."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self._steps: list = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 -- http.server's spelling
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with outer._lock:
                    outer.bodies.append({"path": self.path, "body": body})
                    step = (
                        outer._steps.pop(0)
                        if outer._steps
                        else _respond(*_text("no script"))
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                step(self)

            def log_message(self, *args: Any) -> None:
                return None

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                return None

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def script(self, *steps) -> None:
        self._steps.extend(steps)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ------------------------------------------------------------------ the stack


class _AskUser(BaseTool):
    """The real ``ask_user`` is config-gated and UI-bound (tools.ask_user.enabled);
    headless acceptance stands in a same-named tool so the ladder's
    ``ask_user_calls`` helped-signal is exercised end to end."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return "ask the user a question"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"question": {"type": "string"}}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="用户答复：继续")


class _Stack:
    def __init__(self, server: _FakeModel, tmp_path: Path) -> None:
        self.server = server
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "models:\n"
            "  fake:\n"
            "    name: fake\n"
            f'    base_url: "http://127.0.0.1:{server.port}/v1"\n'
            "    api_key: test\n"
            "    streaming: true\n"
            "    max_tokens: 512\n"
            "    temperature: 0.0\n"
            "    max_retries: 0\n"
            "default_model: fake\n"
            "ai:\n"
            "  max_tool_iterations: 12\n",
            encoding="utf-8",
        )
        app_cfg = ConfigLoader.load_app(str(config_path))
        workspace = tmp_path / "ws"
        workspace.mkdir()
        registry = _build_tool_registry(app_cfg, str(workspace), None)
        registry.register(_AskUser())
        engine = build_engine_from_config(app_cfg)
        self.sessions = SessionService(workspace / "sessions")
        self.loop = _build_loop(
            engine, self.sessions, registry, app_cfg, str(workspace), None
        )
        self.session = self.sessions.create()

    async def close(self) -> None:
        await self.loop.aclose()
        self.server.close()


# --------------------------------------------------------------- inspection


def _messages(body: dict) -> list[dict]:
    return body["body"]["messages"]


def _flat(body: dict) -> str:
    return json.dumps(_messages(body), ensure_ascii=False)


def _conversation(bodies: list[dict]) -> list[dict]:
    """The assembled-conversation requests; the extractor rides its own prompt."""
    return [b for b in bodies if "<methods>" in _messages(b)[0]["content"]]


def _extractors(bodies: list[dict]) -> list[dict]:
    return [b for b in bodies if "记忆抽取器" in _messages(b)[0]["content"]]


def _env_check(body: dict) -> None:
    """13 §2.4: one <environment> block, leading the tail (before task-state)."""
    messages = _messages(body)
    envs = [m for m in messages if "<environment>" in str(m.get("content", ""))]
    assert len(envs) == 1
    env_body = envs[0]["content"]
    assert env_body.startswith("<environment>\n工作区: ")
    assert "日期: " in env_body
    env_at = messages.index(envs[0])
    tasks = [
        at
        for at, m in enumerate(messages)
        if "<task-state>" in str(m.get("content", ""))
    ]
    assert all(at > env_at for at in tasks)


# ------------------------------------------------------------------- the tests


@pytest.mark.asyncio
async def test_the_ladder_closes_its_loop_on_the_wire(tmp_path: Path) -> None:
    server = _FakeModel()
    server.script(
        _respond(*_tool_call("read", READ_MISSING)),   # r1: executed failure
        _respond(*_tool_call("read", READ_MISSING)),   # r2: executed failure -> L1 fires
        _respond(*_tool_call("read", READ_MISSING)),   # r3: duplicate_call denies
        _respond(*_tool_call("read", READ_MISSING)),   # r4: failed_repeat denies -> L2
        _respond(*_tool_call("ask_user", '{"question": "怎么办"}')),  # r5: after L3
        _respond(*_text("完成")),                       # r6: post -> model finished
    )
    stack = _Stack(server, tmp_path)
    try:
        reply = await stack.loop.process_input(stack.session, "读一下 missing.txt")
    finally:
        await stack.close()

    conv = _conversation(server.bodies)
    assert len(conv) == 6
    assert [b["path"] for b in conv] == ["/v1/chat/completions"] * 6

    # A+F: every request carries the methods section and a well-ordered tail.
    for body in conv:
        assert "<methods>" in _messages(body)[0]["content"]
        _env_check(body)

    # D: the ladder is visible in the request sequence, level by level.
    assert "[strategy-gate] 最近 2 轮" in _flat(conv[2])   # r3 carries L1
    assert "直接拦截" in _flat(conv[3])                     # r4 carries L2
    assert "reuse the earlier result" in _flat(conv[3])    # r3's duplicate denial
    assert "change approach" in _flat(conv[4])             # r4's armed refusal
    assert "必须用 ask_user 提问" in _flat(conv[4])         # r5 carries L3

    # The two denied calls never executed: only two real read results exist.
    assert _flat(conv[5]).count("refused by runtime guard") == 2
    assert "change approach" in _flat(conv[5])

    # This ending is the model's own text, not a budget handoff: no closing call,
    # so every conversation request offers tools.
    assert all("tools" in b["body"] for b in conv)
    assert "完成" in reply

    # Persistence closure: the ladder's outcome survives into the session file.
    persisted = stack.sessions.load(stack.session.session_id)
    assert persisted.intervention_state is not None
    strategy = persisted.intervention_state["strategy"]
    assert (strategy["level"], strategy["outcome"]) == (3, "helped")

    # The turn-close chain reached the same wire (evidence, not a stage-1 claim).
    assert len(_extractors(server.bodies)) == 1


@pytest.mark.asyncio
async def test_a_broken_stream_continues_once_on_the_wire(tmp_path: Path) -> None:
    server = _FakeModel()
    server.script(
        _cut_after(_chunk({"content": "前"}), _chunk({"content": "半"})),  # r1 dies
        _respond(*_text("恢复完成")),                                       # r2: the continuation
    )
    stack = _Stack(server, tmp_path)
    try:
        reply = await stack.loop.process_input(stack.session, "说点什么")
    finally:
        await stack.close()

    conv = _conversation(server.bodies)
    assert len(conv) == 2

    # B: the continuation request carries the persisted note and none of the
    # text the caller already saw (13 §2.5 -- the failed step never became a
    # message, so a duplicate of "前半" on the wire would mean it leaked).
    injected = [
        m
        for m in _messages(conv[1])
        if "[stream-interrupted]" in str(m.get("content", ""))
    ]
    assert len(injected) == 1
    assert "前半" not in _flat(conv[1])
    assert "恢复完成" in reply

    notes = [
        m
        for m in stack.session.messages
        if m.synthetic and "[stream-interrupted]" in (m.content or "")
    ]
    assert len(notes) == 1
    persisted = stack.sessions.load(stack.session.session_id)
    assert any(
        m.synthetic and "[stream-interrupted]" in (m.content or "")
        for m in persisted.messages
    )
    assert len(_extractors(server.bodies)) == 1


@pytest.mark.asyncio
async def test_an_empty_step_retries_once_without_changing_the_request(
    tmp_path: Path,
) -> None:
    server = _FakeModel()
    server.script(
        _respond(_chunk({}, finish="stop")),  # r1: no text, no tools, no reasoning
        _respond(*_text("答")),               # r2: the silent retry succeeds
    )
    stack = _Stack(server, tmp_path)
    try:
        reply = await stack.loop.process_input(stack.session, "简单问题")
    finally:
        await stack.close()

    conv = _conversation(server.bodies)
    assert len(conv) == 2

    # H: the retry re-assembles the same request -- nothing new injected
    # (13 §2.7; dsh llm-retry's "retry without changing the request").
    assert len(_messages(conv[0])) == len(_messages(conv[1]))
    assert "[stream-interrupted]" not in _flat(conv[1])
    assert "[strategy-gate]" not in _flat(conv[1])
    assert "答" in reply
    assert len(_extractors(server.bodies)) == 1


@pytest.mark.asyncio
async def test_the_closing_call_offers_no_tools_on_the_wire(tmp_path: Path) -> None:
    server = _FakeModel()
    # 12 distinct successful commands burn the iteration budget (max=12); the
    # ending is the only one that asks for a closing word (G8-2).
    server.script(
        *[
            _respond(*_tool_call("bash", json.dumps({"command": f"echo {i}"})))
            for i in range(12)
        ],
        _respond(*_text("总结")),
    )
    stack = _Stack(server, tmp_path)
    try:
        reply = await stack.loop.process_input(stack.session, "干活")
    finally:
        await stack.close()

    conv = _conversation(server.bodies)
    assert len(conv) == 13  # 12 tool iterations + the closing call

    # G (13 §2.7): the closing request carries the closing prompt and no tools,
    # while every working request offers them.
    closing = conv[-1]
    assert "tools" not in closing["body"]
    assert all("tools" in b["body"] for b in conv[:-1])
    assert _CLOSING_PROMPT in (_messages(closing)[-1].get("content") or "")
    assert "总结" in reply
