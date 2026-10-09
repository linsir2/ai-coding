"""TDD (M0.5): the IPC service + handler table, over a real Unix socket.

Both sides use python-lsp-jsonrpc: the server is ``IpcService`` and the test
client is a plain ``Endpoint`` — the wire format is the library's
Content-Length framed JSON-RPC 2.0.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pylsp_jsonrpc.endpoint import Endpoint
from pylsp_jsonrpc.exceptions import JsonRpcMethodNotFound
from pylsp_jsonrpc.streams import JsonRpcStreamReader, JsonRpcStreamWriter

from ai_coding.server.handlers import make_handlers
from ai_coding.server.service import IpcService
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


class _TestClient:
    """A symmetric endpoint client (same library as the server)."""

    def __init__(self, socket_path: Path) -> None:
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect(str(socket_path))
        self.events: list[dict] = []
        self.turn_finished = threading.Event()
        writer = JsonRpcStreamWriter(self.conn.makefile("wb"))
        self.endpoint = Endpoint({"event": self._on_event}, writer.write)
        reader = JsonRpcStreamReader(self.conn.makefile("rb"))
        self._thread = threading.Thread(
            target=reader.listen, args=(self.endpoint.consume,), daemon=True
        )
        self._thread.start()

    def _on_event(self, params: dict) -> None:
        self.events.append(params)
        if params.get("type") == "turn.finish":
            self.turn_finished.set()

    def request(self, method: str, params: dict | None = None, timeout: float = 5.0):
        return self.endpoint.request(method, params).result(timeout)

    def close(self) -> None:
        self.conn.close()


@pytest_asyncio.fixture
async def ipc(tmp_path):
    world = _fake_world(tmp_path)
    service = IpcService(tmp_path / "ipc.sock", asyncio.get_running_loop())
    service.start()
    service.dispatcher.update(make_handlers(world, service))
    client = _TestClient(tmp_path / "ipc.sock")
    assert service.wait_connected(timeout=5.0)
    try:
        yield world, service, client
    finally:
        client.close()
        service.close()


@pytest.mark.asyncio
async def test_initialize_returns_hello(ipc):
    world, _service, client = ipc
    result = client.request("initialize")
    assert result["workspace"] == world.workspace
    assert result["model"] == "fake-model"
    assert result["sessionId"]
    assert result["models"] == [{"name": "fake-model", "key": "fake"}]


@pytest.mark.asyncio
async def test_unknown_method_answers_error(ipc):
    _world, _service, client = ipc
    with pytest.raises(JsonRpcMethodNotFound):
        client.request("does.not.exist")


@pytest.mark.asyncio
async def test_session_create_and_list(ipc):
    _world, _service, client = ipc
    created = client.request("session.create", {"title": "my session"})
    session_id = created["id"]
    assert session_id
    listed = client.request("session.list")
    ids = [row["id"] for row in listed["sessions"]]
    assert session_id in ids


@pytest.mark.asyncio
async def test_session_prompt_echoes_events(ipc):
    _world, _service, client = ipc
    hello = client.request("initialize")
    session_id = hello["sessionId"]
    ack = client.request("session.prompt", {"id": session_id, "text": "hi"})
    assert ack["turnId"] == 1
    # The echo turn runs on the asyncio loop — poll without blocking it.
    for _ in range(250):
        if client.turn_finished.is_set():
            break
        await asyncio.sleep(0.02)
    assert client.turn_finished.is_set()
    types = [event["type"] for event in client.events]
    assert types[0] == "turn.start"
    assert "text.delta" in types
    final = client.events[-1]["data"]
    assert final["status"] == "completed"
    assert final["text"] == "echo: hi"


@pytest.mark.asyncio
async def test_config_summary(ipc):
    _world, _service, client = ipc
    result = client.request("config.summary")
    assert result["model"] == "fake"
    assert result["maxContextTokens"] == 48000


@pytest.mark.asyncio
async def test_model_set_routes_to_loop(ipc):
    world, _service, client = ipc
    result = client.request("model.set", {"name": "other"})
    assert result["ok"] is True
    assert world.loop.model == "other"


@pytest.mark.asyncio
async def test_server_notify_reaches_the_client(ipc):
    _world, service, client = ipc
    service.notify("event", {"type": "custom", "data": {"x": 1}})
    for _ in range(50):
        if any(event["type"] == "custom" for event in client.events):
            break
        await asyncio.sleep(0.02)
    assert any(event["type"] == "custom" for event in client.events)
