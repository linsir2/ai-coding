"""TDD (M0): the IPC server + handler table, over a real Unix socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from ai_coding.server.handlers import make_handlers
from ai_coding.server.server import IpcServer
from ai_coding.service.session_service import SessionService


class _FakeEngine:
    model_config = SimpleNamespace(name="fake-model")


class _FakeLoop:
    skills = None

    def __init__(self) -> None:
        self.model = ""

    def set_model(self, name: str) -> None:
        self.model = name


def _fake_world(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=SessionService(tmp_path / "sessions"),
        engine=_FakeEngine(),
        config=SimpleNamespace(
            models={"fake": SimpleNamespace(name="fake-model")},
            ai=SimpleNamespace(
                max_context_tokens=48000, max_tool_iterations=10, stop_policy="todo_aware"
            ),
            default_model="fake",
        ),
        loop=_FakeLoop(),
        workspace=str(tmp_path),
        cancel=asyncio.Event(),
    )


async def _call(writer, reader, request_id: int, method: str, params: dict | None = None):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    return json.loads((await reader.readline()).decode())


async def _read(reader) -> dict:
    return json.loads((await reader.readline()).decode())


@pytest_asyncio.fixture
async def ipc(tmp_path):
    handlers: dict = {}
    server = IpcServer(tmp_path / "ipc.sock", handlers)
    world = _fake_world(tmp_path)
    handlers.update(make_handlers(world, server))
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / "ipc.sock"))
    try:
        yield world, server, reader, writer
    finally:
        writer.close()
        await server.stop()


@pytest.mark.asyncio
async def test_initialize_returns_hello(ipc):
    world, server, reader, writer = ipc
    response = await _call(writer, reader, 1, "initialize")
    assert response["id"] == 1
    result = response["result"]
    assert result["workspace"] == world.workspace
    assert result["model"] == "fake-model"
    assert result["sessionId"]
    assert result["models"] == [{"name": "fake-model", "key": "fake"}]


@pytest.mark.asyncio
async def test_unknown_method_answers_method_not_found(ipc):
    _, _, reader, writer = ipc
    response = await _call(writer, reader, 2, "does.not.exist")
    assert response["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_malformed_line_is_dropped_server_survives(ipc):
    _, _, reader, writer = ipc
    writer.write(b"{not json\n")
    await writer.drain()
    error = await _read(reader)
    assert error["error"]["code"] == -32700
    # The connection still answers a real request afterwards.
    response = await _call(writer, reader, 3, "initialize")
    assert "result" in response


@pytest.mark.asyncio
async def test_session_create_and_list(ipc):
    _, _, reader, writer = ipc
    created = await _call(writer, reader, 1, "session.create", {"title": "my session"})
    session_id = created["result"]["id"]
    assert session_id
    listed = await _call(writer, reader, 2, "session.list")
    ids = [row["id"] for row in listed["result"]["sessions"]]
    assert session_id in ids


@pytest.mark.asyncio
async def test_session_prompt_echoes_events(ipc):
    _, _, reader, writer = ipc
    hello = await _call(writer, reader, 1, "initialize")
    session_id = hello["result"]["sessionId"]
    ack = await _call(writer, reader, 2, "session.prompt", {"id": session_id, "text": "hi"})
    assert ack["result"]["turnId"] == 1
    events = []
    while True:
        message = await _read(reader)
        if message.get("method") != "event":
            continue
        event = message["params"]
        events.append(event)
        if event["type"] == "turn.finish":
            break
    assert events[0]["type"] == "turn.start"
    assert any(e["type"] == "text.delta" for e in events)
    final = events[-1]["data"]
    assert final["status"] == "completed"
    assert final["text"] == "echo: hi"


@pytest.mark.asyncio
async def test_config_summary(ipc):
    _, _, reader, writer = ipc
    response = await _call(writer, reader, 1, "config.summary")
    result = response["result"]
    assert result["model"] == "fake"
    assert result["maxContextTokens"] == 48000


@pytest.mark.asyncio
async def test_model_set_routes_to_loop(ipc):
    world, _, reader, writer = ipc
    response = await _call(writer, reader, 1, "model.set", {"name": "other"})
    assert response["result"]["ok"] is True
    assert world.loop.model == "other"


@pytest.mark.asyncio
async def test_server_push_reaches_the_client(ipc):
    _, server, reader, writer = ipc
    await server.push("event", {"type": "custom", "data": {"x": 1}})
    message = await _read(reader)
    assert message["method"] == "event"
    assert message["params"]["type"] == "custom"
