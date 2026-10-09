"""TDD (M0.5/M2): the IPC service + handler table, over a real Unix socket.

Both sides use python-lsp-jsonrpc: the server is ``IpcService`` and the test
client is a plain ``Endpoint`` — the wire format is the library's
Content-Length framed JSON-RPC 2.0.

M2: ``session.prompt`` runs a scripted ``process_input`` (the fake replays
loop events through the bridge), and ``session.interrupt`` must really cancel
the spawned turn — not the M0 stub that set and immediately cleared a flag.
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

from ai_coding.core.events import ToolFinished, ToolStarted, TurnFinished
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.server.handlers import make_handlers
from ai_coding.server.service import IpcService
from ai_coding.service.session_service import SessionService


class _FakeEngine:
    model_config = SimpleNamespace(
        name="fake-model", base_url="https://api.deepseek.com/v1"
    )


class _FakeRegistry:
    """Minimal SkillRegistry stand-in: ``names()`` + ``meta(name)``."""

    def __init__(self, names: list[str], meta: dict[str, dict]) -> None:
        self._names = names
        self._meta = meta

    def names(self) -> list[str]:
        return list(self._names)

    def meta(self, name: str) -> dict:
        return self._meta[name]


class _FakeLoop:
    """Scriptable stand-in for ``AgentLoop.process_input``.

    ``script`` is a list of ``(observer, on_delta) -> None`` steps replayed in
    order; every call is recorded so tests can assert the handler wired the
    bridge into both channels.
    """

    skills = None

    def __init__(self, script: list | None = None) -> None:
        self.model = ""
        self.script = list(script or [])
        self.calls: list[dict] = []

    def set_model(self, name: str) -> None:
        self.model = name

    async def process_input(self, session, user_input, on_delta=None, observer=None):
        self.calls.append(
            {"session": session, "text": user_input, "on_delta": on_delta, "observer": observer}
        )
        for step in self.script:
            step(observer, on_delta)
        return "done"


class _SlowLoop(_FakeLoop):
    """A turn that hangs until cancelled, honouring the loop's cancel contract."""

    async def process_input(self, session, user_input, on_delta=None, observer=None):
        self.calls.append(
            {"session": session, "text": user_input, "on_delta": on_delta, "observer": observer}
        )
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # The real loop emits TurnFinished(ABORTED) and persists before
            # re-raising (core/agent_loop.py:570); the fake mirrors the event
            # so the socket-level test sees the contract, not a stub.
            observer.on_event(
                TurnFinished(outcome=TurnOutcome(status=TurnStatus.ABORTED, reason="interrupted"))
            )
            raise
        return ""


class _BrokenLoop(_FakeLoop):
    """A turn that dies outside the loop's own error handling."""

    async def process_input(self, session, user_input, on_delta=None, observer=None):
        raise RuntimeError("boom")


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
        if params.get("type") == "turn.finished":
            self.turn_finished.set()

    def request(self, method: str, params: dict | None = None, timeout: float = 5.0):
        return self.endpoint.request(method, params).result(timeout)

    async def wait_for(self, predicate, attempts: int = 250) -> bool:
        """Poll without blocking the loop (a threading wait would starve it)."""
        for _ in range(attempts):
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return predicate()

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
    # M3: the models dialog marks the current entry by key.
    assert result["modelKey"] == "fake"
    # M1: the home footer/metadata read these two.
    assert result["provider"] == "deepseek"
    assert result["vcs"] == {"branch": ""}  # tmp_path is not a git repository


@pytest.mark.asyncio
async def test_skills_list_is_structured(ipc):
    world, _service, client = ipc
    world.loop.skills = _FakeRegistry(
        names=["alpha", "beta"],
        meta={
            "alpha": {"name": "alpha", "description": "does alpha", "phase": "planning"},
            "beta": {"name": "beta", "description": None, "phase": ""},
        },
    )
    result = client.request("skills.list")
    assert result == {
        "skills": [
            {"name": "alpha", "description": "does alpha", "phase": "planning"},
            {"name": "beta", "description": None, "phase": ""},
        ]
    }


@pytest.mark.asyncio
async def test_skills_list_empty_without_registry(ipc):
    _world, _service, client = ipc
    assert client.request("skills.list") == {"skills": []}


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
async def test_session_prompt_runs_scripted_turn(ipc):
    world, _service, client = ipc
    hello = client.request("initialize")
    session_id = hello["sessionId"]
    world.loop.script = [
        lambda _obs, delta: delta("hel"),
        lambda _obs, delta: delta("lo"),
        lambda obs, _delta: obs.on_event(
            ToolStarted(call_id="c1", tool_name="bash", params={"command": "ls"}, iteration=1)
        ),
        lambda obs, _delta: obs.on_event(
            ToolFinished(call_id="c1", tool_name="bash", success=True, output_preview="ok")
        ),
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="hello"))
        ),
    ]
    ack = client.request("session.prompt", {"id": session_id, "text": "hi"})
    assert ack["turnId"] == 1
    assert ack["sessionId"] == session_id
    assert await client.wait_for(client.turn_finished.is_set)
    types = [event["type"] for event in client.events]
    assert types == ["text.delta", "text.delta", "tool.started", "tool.finished", "turn.finished"]
    assert client.events[0]["data"] == {"text": "hel"}
    assert client.events[2]["data"]["callId"] == "c1"
    assert client.events[-1]["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_session_prompt_wires_bridge_into_both_channels(ipc):
    """The fake records how it was called: on_delta and observer must be the
    bridge's, so live turns reach the client through the real mapping."""
    world, _service, client = ipc
    client.request("initialize")
    ack = client.request("session.prompt", {"text": "x"})
    assert ack["turnId"] == 1
    assert await client.wait_for(lambda: bool(world.loop.calls))
    call = world.loop.calls[-1]
    assert call["text"] == "x"
    call["on_delta"]("live")
    call["observer"].on_event(
        TurnFinished(outcome=TurnOutcome(status=TurnStatus.ABORTED, reason="stop"))
    )
    assert await client.wait_for(client.turn_finished.is_set)
    types = [event["type"] for event in client.events]
    assert types == ["text.delta", "turn.finished"]
    assert client.events[0]["data"] == {"text": "live"}
    assert client.events[1]["data"]["status"] == "aborted"


@pytest.mark.asyncio
async def test_session_prompt_requires_initialize(ipc):
    _world, _service, client = ipc
    with pytest.raises(Exception, match="session"):
        client.request("session.prompt", {"text": "no session yet"})


@pytest.mark.asyncio
async def test_interrupt_cancels_running_turn(ipc):
    world, _service, client = ipc
    client.request("initialize")
    world.loop = _SlowLoop()
    client.request("session.prompt", {"text": "slow"})
    assert await client.wait_for(lambda: bool(world.loop.calls))
    ack = client.request("session.interrupt")
    assert ack["ok"] is True
    assert ack["interrupted"] == 1
    assert await client.wait_for(client.turn_finished.is_set)
    assert client.events[-1]["data"]["status"] == "aborted"
    # The tool-batch fast path: one Esc leaves the event set; the loop clears
    # it before each batch (core/agent_loop.py:932).
    assert world.cancel.is_set()


@pytest.mark.asyncio
async def test_interrupt_with_no_turn_is_a_noop(ipc):
    _world, _service, client = ipc
    client.request("initialize")
    assert client.request("session.interrupt") == {"ok": True, "interrupted": 0}


@pytest.mark.asyncio
async def test_turn_runner_reports_unexpected_errors(ipc):
    world, _service, client = ipc
    client.request("initialize")
    world.loop = _BrokenLoop()
    client.request("session.prompt", {"text": "boom"})
    assert await client.wait_for(client.turn_finished.is_set)
    final = client.events[-1]["data"]
    assert final["status"] == "error"
    assert "boom" in final["reason"]


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
    # M3: the metadata label follows the newly active model's endpoint.
    assert result["provider"] == "deepseek"


@pytest.mark.asyncio
async def test_server_notify_reaches_the_client(ipc):
    _world, service, client = ipc
    service.notify("event", {"type": "custom", "data": {"x": 1}})
    for _ in range(50):
        if any(event["type"] == "custom" for event in client.events):
            break
        await asyncio.sleep(0.02)
    assert any(event["type"] == "custom" for event in client.events)
