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
from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.domain.session import TurnUsage
from ai_coding.security.approval import ApprovalResult, CLIApprovalCallback
from ai_coding.security.user_prompt import CLIUserPromptCallback
from ai_coding.server.handlers import make_handlers
from ai_coding.server.service import IpcService
from ai_coding.service.session_service import SessionService


class _FakeEngine:
    model_config = SimpleNamespace(
        name="fake-model", base_url="https://api.deepseek.com/v1"
    )


class _FakeRegistry:
    """Minimal SkillRegistry stand-in: the parsed skills, not a metadata dict.

    The handler reads ``all()`` and takes the fields off the object, because
    that is the one parse every consumer now shares.
    """

    def __init__(self, skills: list) -> None:
        self._skills = skills

    def all(self) -> tuple:
        return tuple(self._skills)

    def names(self) -> list[str]:
        return [skill.id for skill in self._skills]


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
        # The real loop persists the exchange; commands that rewrite history
        # (``/compact``) need something to rewrite.
        from ai_coding.domain.run import assistant_message, user_message

        stamp = "2026-01-01T00:00:00Z"
        session.add_message(user_message(user_input, stamp))
        session.add_message(assistant_message(StepResult(text="done"), stamp))
        return "done"

    async def compact_session(self, session):
        """The real loop replaces the stored history (compaction.py:101)."""
        before = len(session.messages)
        session.messages = list(session.messages)[-1:]
        return {"messages": before, "after": len(session.messages), "removed": before - 1}

    async def resume_turn(self, session, on_delta=None, observer=None):
        """Mirror the real contract (08/Q1): a resumed turn appends no user
        message -- only the model's answer lands in the history."""
        self.calls.append(
            {"session": session, "resumed": True, "on_delta": on_delta, "observer": observer}
        )
        for step in self.script:
            step(observer, on_delta)
        from ai_coding.domain.run import assistant_message

        stamp = "2026-01-01T00:00:00Z"
        session.add_message(assistant_message(StepResult(text="done"), stamp))
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
    # Faithful in one respect the /permissions invariant depends on: a real
    # AgentLoop hands the very callback world holds to its dispatcher, so the
    # command layer can swap ``dispatcher.approval`` out from under it (I1).
    approval = CLIApprovalCallback()
    loop = _FakeLoop()
    loop.dispatcher = SimpleNamespace(approval=approval, gate=None)
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
        loop=loop,
        workspace=str(tmp_path),
        cancel=asyncio.Event(),
        approval=approval,
        user_prompt=CLIUserPromptCallback(),
    )


class _TestClient:
    """A symmetric endpoint client (same library as the server)."""

    def __init__(self, socket_path: Path) -> None:
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect(str(socket_path))
        self.events: list[dict] = []
        self.turn_finished = threading.Event()
        self.approval_answer = "yes"
        self.approval_requests: list[dict] = []
        self.ask_answer = "自定义回答"
        self.ask_requests: list[dict] = []
        writer = JsonRpcStreamWriter(self.conn.makefile("wb"))
        self.endpoint = Endpoint(
            {
                "event": self._on_event,
                "approval.request": self._on_approval,
                "user_prompt.ask": self._on_ask,
            },
            writer.write,
        )
        reader = JsonRpcStreamReader(self.conn.makefile("rb"))
        self._thread = threading.Thread(
            target=reader.listen, args=(self.endpoint.consume,), daemon=True
        )
        self._thread.start()

    def _on_event(self, params: dict) -> None:
        self.events.append(params)
        if params.get("type") == "turn.finished":
            self.turn_finished.set()

    def _on_approval(self, params: dict) -> str:
        """Answer a server-initiated approval request (M5)."""
        self.approval_requests.append(dict(params or {}))
        return self.approval_answer

    def _on_ask(self, params: dict) -> str:
        """Answer the model's question (M6.5): the text the tool renders."""
        self.ask_requests.append(dict(params or {}))
        return self.ask_answer

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
    from ai_coding.skills.registry import Skill

    world.loop.skills = _FakeRegistry(
        [
            Skill(id="alpha", body="", path=Path("."), description="does alpha", phase="planning"),
            Skill(id="beta", body="", path=Path(".")),
        ]
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
async def test_session_command_routes_through_handle_input(ipc):
    """A slash command goes through the REPL dispatcher, not to the model.

    ``handle_input`` is the backend's command design (app/commands.py); the
    TUI's entry is one generic ``session.command`` — the console lines the
    dispatcher prints arrive as ``console.line`` events, and the run ends
    once, with the action in the terminal event's data.
    """
    _world, _service, client = ipc
    client.request("initialize")
    ack = client.request("session.command", {"text": "/help"})
    assert ack["sessionId"]
    assert await client.wait_for(client.turn_finished.is_set)

    types = [event["type"] for event in client.events]
    assert "console.line" in types, "the command's output must reach the client"
    assert types[-1] == "turn.finished"
    assert client.events[-1]["data"]["status"] == "completed"
    assert client.events[-1]["data"]["action"] == "continue"


@pytest.mark.asyncio
async def test_session_command_inner_turns_are_steps_not_endings(ipc):
    """A command that runs turns (/review, /plan steps) drives one run.

    The loop's inner ``TurnFinished`` is a *step* of the command — the same
    assistant block the old TUI kept open — and the run's single terminal
    ``turn.finished`` is emitted after ``handle_input`` returns, so the queue
    and the busy flag settle exactly once.
    """
    world, _service, client = ipc
    client.request("initialize")
    world.loop.script = [
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="done"))
        )
    ]
    client.request("session.command", {"text": "/review"})
    assert await client.wait_for(client.turn_finished.is_set)

    types = [event["type"] for event in client.events]
    assert types.count("turn.step") == 1
    assert types.count("turn.finished") == 1
    assert types[-1] == "turn.finished"
    # the observer reached the turn: the step event exists at all
    assert client.events[-1]["data"]["action"] == "continue"


@pytest.mark.asyncio
async def test_session_command_quit_reports_the_action(ipc):
    _world, _service, client = ipc
    client.request("initialize")
    client.request("session.command", {"text": "/quit"})
    assert await client.wait_for(client.turn_finished.is_set)
    assert client.events[-1]["data"]["action"] == "quit"


@pytest.mark.asyncio
async def test_permissions_confirm_keeps_approvals_on_the_wire(ipc):
    """I1: ``/permissions confirm`` swaps in a FRESH CLIApprovalCallback with no
    provider — without re-installing the IPC provider afterwards the next WARN
    approval falls back to reading stdin and the turn hangs (the retired TUI
    called ``_ensure_approval_provider`` after every command for this reason).
    """
    from ai_coding.security.approval import ApprovalResult

    world, _service, client = ipc
    client.request("initialize")
    client.request("session.command", {"text": "/permissions confirm"})
    assert await client.wait_for(client.turn_finished.is_set)

    # the swapped callback must still answer over the socket, not stdin
    client.approval_answer = "yes"
    result = await world.loop.dispatcher.approval.approve(
        "write", {"path": "b.py"}, "warn tier"
    )
    assert result is ApprovalResult.YES
    assert client.approval_requests[-1]["toolName"] == "write"


@pytest.mark.asyncio
async def test_session_command_inner_turns_end_once_when_cancelled(ipc):
    """I3 on the cancel path: exactly one terminal event, status aborted."""
    world, _service, client = ipc
    client.request("initialize")
    world.loop = _SlowLoop()
    client.request("session.command", {"text": "/review"})
    assert await client.wait_for(lambda: bool(world.loop.calls))
    client.request("session.interrupt")
    assert await client.wait_for(client.turn_finished.is_set)

    types = [event["type"] for event in client.events]
    assert types.count("turn.finished") == 1
    assert client.events[-1]["data"]["status"] == "aborted"


@pytest.mark.asyncio
async def test_compact_reports_in_place_without_re_projection(ipc):
    """P3-4: the transcript is cumulative — live rows stay where they landed.
    A command that rewrote the history in the same session reports the fact
    (`compacted`) for the toast instead of asking for a re-projection that
    would wipe the live compaction row.  Only a session switch reloads."""
    world, _service, client = ipc
    client.request("initialize")
    # give the session something to compact
    world.loop.script = [
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="done"))
        )
    ]
    client.request("session.prompt", {"text": "one"})
    assert await client.wait_for(client.turn_finished.is_set)
    client.turn_finished.clear()
    client.events.clear()

    client.request("session.command", {"text": "/compact"})
    assert await client.wait_for(client.turn_finished.is_set)

    data = client.events[-1]["data"]
    assert data["reload"] is False
    assert data["compacted"]["after"] < data["compacted"]["before"]


@pytest.mark.asyncio
async def test_clear_switching_session_still_asks_for_re_projection(ipc):
    """The reload survives for its remaining purpose: /clear moves the live
    session, so the client must re-project onto the new one."""
    _world, _service, client = ipc
    hello = client.request("initialize")

    client.request("session.command", {"text": "/clear"})
    assert await client.wait_for(client.turn_finished.is_set)

    data = client.events[-1]["data"]
    assert data["reload"] is True
    assert data["sessionId"] != hello["sessionId"]
    assert data.get("compacted") is None


@pytest.mark.asyncio
async def test_a_query_command_asks_for_no_re_projection(ipc):
    _world, _service, client = ipc
    client.request("initialize")
    client.request("session.command", {"text": "/status"})
    assert await client.wait_for(client.turn_finished.is_set)
    assert client.events[-1]["data"]["reload"] is False


@pytest.mark.asyncio
async def test_deleting_the_live_session_switches_to_a_new_one(ipc):
    """J6: deleting the session the turn runs against must move the live
    session, or the next turn persists into a deleted file (recreating it)."""
    _world, _service, client = ipc
    hello = client.request("initialize")
    result = client.request("session.delete", {"id": hello["sessionId"]})
    assert result["ok"] is True
    assert result["sessionId"] and result["sessionId"] != hello["sessionId"]

    listed = client.request("session.list")
    assert all(row["id"] != hello["sessionId"] for row in listed["sessions"])


@pytest.mark.asyncio
async def test_deleting_another_session_leaves_the_live_one(ipc):
    """Deleting a session that is *not* live does not move the live session.

    ``session.create`` persists and makes live, so the middle of three is the
    one that is on disk and not live — the only shape a client can delete
    without the J6 switch firing.
    """
    _world, _service, client = ipc
    client.request("initialize")
    spare = client.request("session.create", {"title": "spare"})
    live = client.request("session.create", {"title": "live"})

    result = client.request("session.delete", {"id": spare["id"]})
    assert result["ok"] is True
    assert result["sessionId"] == ""  # the live session did not move

    listed = client.request("session.list")["sessions"]
    ids = {row["id"] for row in listed}
    assert spare["id"] not in ids and live["id"] in ids


@pytest.mark.asyncio
async def test_deleting_an_unknown_session_is_a_no_op(ipc):
    _world, _service, client = ipc
    client.request("initialize")
    assert client.request("session.delete", {"id": "nope"})["ok"] is False


@pytest.mark.asyncio
async def test_mcp_list_reports_what_became_of_the_servers(ipc):
    """T5: the dialog's rows, over the real IPC the front end uses."""
    world, _service, client = ipc
    world.mcp = SimpleNamespace(servers=lambda: [
        {"name": "fs", "command": "mcp-fs", "status": "connected", "toolCount": 2,
         "wantedCount": 0, "detail": ""},
        {"name": "dead", "command": "nope", "status": "failed", "toolCount": 0,
         "wantedCount": 0, "detail": "spawn failed"},
    ])
    result = client.request("mcp.list")
    assert [row["name"] for row in result["servers"]] == ["fs", "dead"]
    assert result["servers"][0]["status"] == "connected"


@pytest.mark.asyncio
async def test_mcp_connect_wakes_the_manager_for_that_server(ipc):
    """M3: the panel's failed row has one action, and it reaches the manager."""
    world, _service, client = ipc
    woken: list[str] = []
    world.mcp = SimpleNamespace(request_connect=lambda name: woken.append(name) or True)

    assert client.request("mcp.connect", {"server": "fs"}) == {"ok": True}
    assert woken == ["fs"]


@pytest.mark.asyncio
async def test_mcp_connect_answers_false_for_a_server_that_cannot_connect(ipc):
    world, _service, client = ipc
    world.mcp = SimpleNamespace(request_connect=lambda name: False)

    result = client.request("mcp.connect", {"server": "ghost"})

    assert result["ok"] is False
    assert "ghost" in result["reason"]


@pytest.mark.asyncio
async def test_mcp_connect_with_no_manager_is_an_error_not_a_crash(ipc):
    _world, _service, client = ipc
    result = client.request("mcp.connect", {"server": "fs"})
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_mcp_login_reaches_the_managers_login(ipc):
    """M4: the panel's sign-in action is a real login, and the world is the
    only place that knows the manager."""
    world, _service, client = ipc
    started: list[str] = []

    async def login(name: str, timeout: float = 300.0):
        started.append(name)
        return {"ok": True}

    world.mcp = SimpleNamespace(login=login, logout=lambda name: {"ok": True})

    assert client.request("mcp.login", {"server": "github"}) == {"ok": True}
    # The reply means "started": the login itself is spawned, so the call lands
    # on the next loop pass — the same shape session.turn uses.
    await asyncio.sleep(0.05)
    assert started == ["github"]


@pytest.mark.asyncio
async def test_mcp_logout_answers_with_the_managers_verdict(ipc):
    world, _service, client = ipc
    world.mcp = SimpleNamespace(
        login=None, logout=lambda name: {"ok": False, "reason": "nope"}
    )

    result = client.request("mcp.logout", {"server": "ghost"})

    assert result == {"ok": False, "reason": "nope"}


@pytest.mark.asyncio
async def test_mcp_list_says_whether_mcp_is_switched_on(ipc):
    """M5 follow-up: the panel has to be able to tell the operator *before*
    they type anything that the master switch is off.

    Without this the wizard collects three answers and only then refuses, which
    is what a real operator reported: ``MCP is disabled (mcp.enabled is
    false)`` arriving after they had already chosen kind, name, command and
    trust.  ``adopt`` stays the authority; this is the panel's chance not to
    waste the steps.
    """
    world, _service, client = ipc
    world.mcp = SimpleNamespace(enabled=False, servers=lambda: [])

    listed = client.request("mcp.list", {})

    assert listed["enabled"] is False
    assert listed["servers"] == []


@pytest.mark.asyncio
async def test_mcp_login_with_no_manager_is_an_error_not_a_crash(ipc):
    _world, _service, client = ipc
    assert client.request("mcp.login", {"server": "fs"})["ok"] is False
    assert client.request("mcp.logout", {"server": "fs"})["ok"] is False


@pytest.mark.asyncio
async def test_mcp_login_without_a_name_is_an_error(ipc):
    world, _service, client = ipc
    world.mcp = SimpleNamespace(login=None, logout=lambda name: {"ok": True})
    assert client.request("mcp.login", {})["ok"] is False


@pytest.mark.asyncio
async def test_mcp_add_adopts_and_persists_the_wizards_server(ipc, tmp_path):
    """M5: the guided add is one round trip — adopt on the live manager, then
    write the sidecar so a restart reconnects the same server."""
    world, _service, client = ipc
    adopted: list[dict] = []
    started: list[str] = []

    async def start_supervisor(server):
        started.append(server.name)

    world.mcp = SimpleNamespace(
        adopt=lambda server: adopted.append(server.model_dump()) or {"ok": True},
        start_supervisor=start_supervisor,
        login=None,
        logout=lambda name: {"ok": True},
    )
    world.config_path = str(tmp_path / "config.local.yaml")
    (tmp_path / "config.local.yaml").write_text("mcp:\n  enabled: true\n", encoding="utf-8")

    result = client.request(
        "mcp.add",
        {"name": "fs", "type": "local", "command": ["python", "-m", "srv"], "auto_approve": True},
    )

    assert result["ok"] is True
    assert result["saved"].endswith("mcp.local.yaml")
    assert adopted[0]["name"] == "fs"
    assert adopted[0]["command"] == ["python", "-m", "srv"]
    # And a supervisor was started for it, on the loop that serves IPC rather
    # than one this thread has no right to create (M3 owns the retry/backoff).
    # It is spawned, not awaited, so give the serve loop a turn to run it.
    for _ in range(20):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started == ["fs"]
    # And it is on disk, so the next start loads it.
    from ai_coding.config.loader import ConfigLoader

    assert [s.name for s in ConfigLoader.load_mcp(tmp_path / "config.local.yaml").servers] == ["fs"]


@pytest.mark.asyncio
async def test_mcp_add_refuses_what_the_manager_refuses(ipc):
    world, _service, client = ipc
    world.mcp = SimpleNamespace(
        adopt=lambda server: {"ok": False, "reason": "already exists"},
        login=None,
        logout=lambda name: {"ok": True},
    )
    world.config_path = None

    result = client.request("mcp.add", {"name": "fs", "type": "local", "command": ["x"]})

    assert result == {"ok": False, "reason": "already exists"}


@pytest.mark.asyncio
async def test_mcp_add_with_no_manager_is_an_error_not_a_crash(ipc):
    _world, _service, client = ipc
    assert client.request("mcp.add", {"name": "fs"})["ok"] is False


@pytest.mark.asyncio
async def test_mcp_add_refuses_a_name_the_tools_cannot_carry(ipc):
    """The name becomes `server__tool`; a space in it would be a tool name no
    provider accepts, so the backend refuses it before adopting."""
    world, _service, client = ipc
    adopted: list[str] = []
    world.mcp = SimpleNamespace(adopt=lambda server: adopted.append(server.name) or {"ok": True})

    for bad in ("f fs", "zzfs.venv/bin", "a/b"):
        result = client.request("mcp.add", {"name": bad, "type": "local", "command": ["x"]})
        assert result["ok"] is False, bad
        assert "letters" in result["reason"]
    assert adopted == []


@pytest.mark.asyncio
async def test_mcp_add_without_a_name_is_an_error(ipc):
    world, _service, client = ipc
    world.mcp = SimpleNamespace(adopt=lambda server: {"ok": True})
    assert client.request("mcp.add", {"type": "local", "command": ["x"]})["ok"] is False


@pytest.mark.asyncio
async def test_mcp_list_with_no_manager_is_an_empty_list_not_an_error(ipc):
    """T5b: a world built before mcp existed (or a test's fake world) must not
    turn a read-only panel into a crash."""
    _world, _service, client = ipc
    # The shared fixture has no `mcp` attribute at all.
    client.request("initialize")
    assert client.request("mcp.list") == {"servers": [], "enabled": False}


@pytest.mark.asyncio
async def test_permissions_state_reports_policy_grants_and_store(ipc):
    """The dialog's read side: current policy, remembered grants, store path.

    Read-only on purpose — every mutation goes through the command channel so
    the provider re-arm (I1) has exactly one home.
    """
    world, _service, client = ipc
    client.request("initialize")

    state = client.request("permissions.state")
    assert state["policy"] == "confirm"  # the fake world ships CLIApprovalCallback
    assert state["grants"] == []
    assert state["storePath"] == ""      # no store in the fake gate

    client.request("session.command", {"text": "/permissions auto"})
    assert await client.wait_for(client.turn_finished.is_set)
    client.turn_finished.clear()
    assert client.request("permissions.state")["policy"] == "auto"
    assert world.loop.dispatcher.approval.__class__.__name__ == "AutoApproveCallback"


@pytest.mark.asyncio
async def test_session_command_new_session_switches_the_live_session(ipc):
    """/clear ends with the REPL's NEW_SESSION action: the live session
    follows the dispatcher, and the terminal event names the new one so the
    tab strip can switch (and the session is listable)."""
    _world, _service, client = ipc
    hello = client.request("initialize")
    client.request("session.command", {"text": "/clear"})
    assert await client.wait_for(client.turn_finished.is_set)

    final = client.events[-1]["data"]
    assert final["action"] == "new_session"
    assert final["sessionId"] and final["sessionId"] != hello["sessionId"]
    listed = client.request("session.list")
    assert any(row["id"] == final["sessionId"] for row in listed["sessions"])


@pytest.mark.asyncio
async def test_session_command_interrupt_ends_the_run_aborted(ipc):
    world, _service, client = ipc
    client.request("initialize")
    world.loop = _SlowLoop()
    client.request("session.command", {"text": "/review"})
    assert await client.wait_for(lambda: bool(world.loop.calls))
    client.request("session.interrupt")
    assert await client.wait_for(client.turn_finished.is_set)
    assert client.events[-1]["data"]["status"] == "aborted"


@pytest.mark.asyncio
async def test_a_rejected_paste_is_reported_in_the_prompt_ack(ipc):
    """r20/F5.  The transcript draws the pasted picture before the request is
    sent, so a refusal in the handler left the user looking at an image the
    model never received -- and the ack said nothing at all.  The ack is the
    response the client already awaits, so this costs no new IPC call."""
    world, _service, client = ipc
    hello = client.request("initialize")
    session_id = hello["sessionId"]
    world.loop.script = [
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="ok"))
        ),
    ]
    ack = client.request("session.prompt", {
        "id": session_id,
        "text": "看这个",
        "images": [{"name": "shot.png", "data_url": "data:text/plain;base64,aGk="}],
    })

    assert ack["rejected"], f"a refused paste was dropped in silence: {ack}"
    assert ack["rejected"][0]["name"] == "shot.png"
    assert "text/plain" in ack["rejected"][0]["reason"], ack["rejected"]


@pytest.mark.asyncio
async def test_an_accepted_paste_reports_no_rejection(ipc):
    """The new field must not make every ordinary paste look suspicious."""
    import base64

    from tests.integration.test_r17_dataflow import PNG_1PX

    world, _service, client = ipc
    hello = client.request("initialize")
    session_id = hello["sessionId"]
    world.loop.script = [
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="ok"))
        ),
    ]
    png = base64.b64encode(PNG_1PX).decode()
    ack = client.request("session.prompt", {
        "id": session_id,
        "text": "看这个",
        "images": [{"name": "shot.png", "data_url": f"data:image/png;base64,{png}"}],
    })

    assert ack["rejected"] == [], ack


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
async def test_approval_round_trip_over_the_wire(ipc):
    """M5: a WARN call's prompt travels as a server-initiated JSON-RPC request.

    The client's answer maps through the CLI vocabulary the shared parser
    already speaks (security/approval.py:88-98).
    """
    world, _service, client = ipc
    client.approval_answer = "always"
    result = await world.approval.approve("write", {"path": "a.py"}, "warn tier")
    assert result is ApprovalResult.ALWAYS
    assert client.approval_requests[-1] == {
        "toolName": "write",
        "params": {"path": "a.py"},
        "reason": "warn tier",
    }


@pytest.mark.asyncio
async def test_approval_fails_closed_on_an_unknown_answer(ipc):
    world, _service, client = ipc
    client.approval_answer = "???"
    assert await world.approval.approve("bash", {"command": "ls"}, "warn") is ApprovalResult.NO


@pytest.mark.asyncio
async def test_user_prompt_round_trip_over_the_wire(ipc):
    """The model's question travels as a server-initiated JSON-RPC request.

    Same shape as the approval channel; the answer comes back as the plain
    text the tool renders to the model (``用户回答：…``).
    """
    from ai_coding.security.user_prompt import UserAnswer

    world, _service, client = ipc
    client.ask_answer = "第二个"
    answer = await world.user_prompt.ask("哪个文件？", ["a.py", "b.py"])
    assert answer == UserAnswer(answered=True, text="第二个")
    assert client.ask_requests[-1] == {
        "question": "哪个文件？",
        "options": ["a.py", "b.py"],
    }


@pytest.mark.asyncio
async def test_user_prompt_an_empty_answer_is_not_an_answer(ipc):
    """esc / a closed panel returns "": the model must decide itself."""
    from ai_coding.security.user_prompt import UserAnswer

    world, _service, client = ipc
    client.ask_answer = ""
    answer = await world.user_prompt.ask("确认吗？", ["yes", "no"])
    assert answer == UserAnswer(answered=False, reason="empty")


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
async def test_session_stats_without_usage(ipc):
    _world, _service, client = ipc
    client.request("initialize")
    assert client.request("session.stats") == {"tokens": 0, "maxContextTokens": 48000}


@pytest.mark.asyncio
async def test_session_stats_reports_last_turn_prompt_tokens(ipc):
    world, _service, client = ipc
    client.request("initialize")
    client.request("session.prompt", {"text": "x"})
    assert await client.wait_for(lambda: bool(world.loop.calls))
    session = world.loop.calls[-1]["session"]
    session.turn_usages.append(
        TurnUsage(turn=1, prompt_tokens=1234, completion_tokens=56, total_tokens=1290)
    )
    assert client.request("session.stats") == {"tokens": 1234, "maxContextTokens": 48000}

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


@pytest.mark.asyncio
async def test_mcp_add_carries_a_manual_token_into_the_server_headers(ipc, tmp_path):
    """M7: GitHub's remote MCP endpoint refuses dynamic client registration
    ("Dynamic Client Registration is NOT supported by Remote GitHub MCP
    Server"), so OAuth can never authenticate it. The universal path every
    client uses is a static ``Authorization: Bearer`` header — so the wizard
    has to be able to collect one."""
    world, _service, client = ipc
    adopted: list[dict] = []

    async def start_supervisor(server):
        return True

    world.mcp = SimpleNamespace(
        adopt=lambda server: adopted.append(server.model_dump()) or {"ok": True},
        start_supervisor=start_supervisor,
        login=None,
        logout=lambda name: {"ok": True},
    )
    world.config_path = str(tmp_path / "config.local.yaml")
    (tmp_path / "config.local.yaml").write_text("mcp:\n  enabled: true\n", encoding="utf-8")

    result = client.request(
        "mcp.add",
        {
            "name": "github",
            "type": "remote",
            "url": "https://api.githubcopilot.com/mcp/",
            "token": "ghp_example",
            "auto_approve": False,
        },
    )

    assert result["ok"] is True
    assert adopted[0]["headers"] == {"Authorization": "Bearer ghp_example"}
    assert adopted[0]["url"] == "https://api.githubcopilot.com/mcp/"


@pytest.mark.asyncio
async def test_a_token_is_never_echoed_back_in_a_failure(ipc, tmp_path):
    """A refusal must not quote the secret the operator just typed."""
    world, _service, client = ipc
    world.mcp = SimpleNamespace(
        adopt=lambda server: {"ok": False, "reason": "that url is not usable"},
        login=None,
        logout=lambda name: {"ok": True},
    )
    world.config_path = str(tmp_path / "config.local.yaml")
    (tmp_path / "config.local.yaml").write_text("mcp:\n  enabled: true\n", encoding="utf-8")

    result = client.request(
        "mcp.add",
        {"name": "gh", "type": "remote", "url": "https://x/", "token": "ghp_secret_value"},
    )

    assert result["ok"] is False
    assert "ghp_secret_value" not in str(result)


@pytest.mark.asyncio
async def test_no_token_means_no_authorization_header(ipc, tmp_path):
    """Omitting the token must leave the OAuth path intact, not send an empty
    bearer that would look like bad credentials."""
    world, _service, client = ipc
    adopted: list[dict] = []

    async def start_supervisor(server):
        return True

    world.mcp = SimpleNamespace(
        adopt=lambda server: adopted.append(server.model_dump()) or {"ok": True},
        start_supervisor=start_supervisor,
        login=None,
        logout=lambda name: {"ok": True},
    )
    world.config_path = str(tmp_path / "config.local.yaml")
    (tmp_path / "config.local.yaml").write_text("mcp:\n  enabled: true\n", encoding="utf-8")

    client.request(
        "mcp.add",
        {"name": "gh", "type": "remote", "url": "https://x/", "auto_approve": False},
    )

    assert adopted[0]["headers"] == {}


# ---- memory: what the agent already knows, made visible ---------------------
#
# ``/memory`` printed one line of counts (43 notes, 33 scene labels) and nothing
# else: the operator could not read a single memory, could not pin one, and
# could not remove one they had taught it wrongly.


class _Note:
    """The attribute surface the rows read off a real MemoryNote."""

    def __init__(self, row):
        self.id = row["id"]
        self.text = row["text"]
        self.kind = row.get("kind", "fact")
        self.scene = row.get("scene", "")
        self.pinned = bool(row.get("pinned"))
        self.importance = row.get("importance", 3)
        self.evidence = row.get("evidence", "")
        self.hits = row.get("hits", 0)
        self.updated_time = row.get("updated", "")


class _MemoryDouble:
    """Stands in for the memory facade the loop hands the panel."""

    def __init__(self, notes):
        self._notes = {row["id"]: row for row in notes}

    def list_rows(self):
        return [_Note(row) for row in self._notes.values()]

    def pin(self, note_id, pinned):
        row = self._notes.get(note_id)
        if row is None:
            return None
        row["pinned"] = bool(pinned)
        return _Note(row)

    def delete(self, note_id):
        return self._notes.pop(note_id, None) is not None

    def status(self):
        return {"total": len(self._notes), "active": len(self._notes), "pinned": 0, "enabled": True}


def _with_memory(client_notes):
    def _attach(world):
        world.loop.turn_memory = _MemoryDouble(client_notes)

    return _attach


@pytest.mark.asyncio
async def test_memory_list_shows_every_note_not_just_counts(ipc):
    world, _service, client = ipc
    world.loop.turn_memory = _MemoryDouble(
        [
            {"id": "n1", "text": "用户偏好中文回答", "kind": "preference", "scene": "写作偏好"},
            {"id": "n2", "text": "MCP 采用单壳分发", "kind": "decision", "evidence": "tool"},
        ]
    )

    result = client.request("memory.list")

    assert [note["text"] for note in result["notes"]] == ["用户偏好中文回答", "MCP 采用单壳分发"]
    assert result["notes"][1]["evidence"] == "tool"
    assert result["enabled"] is True
    assert result["stats"]["total"] == 2


@pytest.mark.asyncio
async def test_memory_pin_and_unpin_round_trip(ipc):
    world, _service, client = ipc
    world.loop.turn_memory = _MemoryDouble([{"id": "n1", "text": "x"}])

    assert client.request("memory.pin", {"id": "n1", "pinned": True})["ok"] is True
    assert client.request("memory.list")["notes"][0]["pinned"] is True
    assert client.request("memory.pin", {"id": "n1", "pinned": False})["ok"] is True
    assert client.request("memory.list")["notes"][0]["pinned"] is False


@pytest.mark.asyncio
async def test_pinning_or_deleting_a_note_that_does_not_exist_says_so(ipc):
    world, _service, client = ipc
    world.loop.turn_memory = _MemoryDouble([{"id": "n1", "text": "x"}])

    assert client.request("memory.pin", {"id": "gone", "pinned": True})["ok"] is False
    assert client.request("memory.delete", {"id": "gone"})["ok"] is False


@pytest.mark.asyncio
async def test_memory_delete_removes_exactly_one_note(ipc):
    world, _service, client = ipc
    world.loop.turn_memory = _MemoryDouble(
        [{"id": "n1", "text": "keep"}, {"id": "n2", "text": "drop"}]
    )

    assert client.request("memory.delete", {"id": "n2"})["ok"] is True
    assert [note["id"] for note in client.request("memory.list")["notes"]] == ["n1"]


@pytest.mark.asyncio
async def test_memory_handlers_are_hands_off_without_a_store(ipc):
    """A world whose loop has no memory (or no turn_memory at all) must answer,
    not raise — the same rule the MCP panel follows."""
    _world, _service, client = ipc

    assert client.request("memory.list")["notes"] == []
    assert client.request("memory.pin", {"id": "x", "pinned": True})["ok"] is False
    assert client.request("memory.delete", {"id": "x"})["ok"] is False


@pytest.mark.asyncio
async def test_pending_skill_approval_round_trip(ipc, tmp_path):
    """The review stages into `.pending/`; the human moves it in or drops it."""
    from ai_coding.core.skill_review import stage_skill
    from ai_coding.skills.registry import SkillRegistry

    world, _service, client = ipc
    registry = SkillRegistry(tmp_path)
    world.loop.skills = registry
    stage_skill(tmp_path, "debug-pytest", "---\nname: debug-pytest\ndescription: d\n---\nb\n",
                reason="踩坑三次")

    listed = client.request("skill.pending.list")
    assert listed == {"pending": [{"name": "debug-pytest", "reason": "踩坑三次"}]}
    assert registry.names() == [], "staged means not loadable"

    assert client.request("skill.pending.approve", {"name": "debug-pytest"}) == {
        "ok": True,
        "name": "debug-pytest",
    }
    assert registry.names() == ["debug-pytest"]


@pytest.mark.asyncio
async def test_pending_skill_rejection(ipc, tmp_path):
    from ai_coding.core.skill_review import stage_skill
    from ai_coding.skills.registry import SkillRegistry

    world, _service, client = ipc
    world.loop.skills = SkillRegistry(tmp_path)
    stage_skill(tmp_path, "junk", "---\nname: junk\ndescription: d\n---\nb\n")

    assert client.request("skill.pending.reject", {"name": "junk"})["ok"] is True
    assert client.request("skill.pending.list") == {"pending": []}


@pytest.mark.asyncio
async def test_approving_an_unknown_pending_skill_is_not_ok(ipc, tmp_path):
    from ai_coding.skills.registry import SkillRegistry

    world, _service, client = ipc
    world.loop.skills = SkillRegistry(tmp_path)

    assert client.request("skill.pending.approve", {"name": "nothing"})["ok"] is False


# ---- P5: the recovery facts reach the client through session.load ------------


@pytest.mark.asyncio
async def test_session_load_reports_a_recovered_crash(ipc):
    """A file that says a turn was open is the crash signal; the client has to
    be told, or the notice would exist only in the log."""
    world, _service, client = ipc
    session = world.sessions.create()
    session.open_turn = 3
    world.sessions.save(session)

    result = client.request("session.load", {"id": session.session_id})

    assert result["recoveredInterrupted"] is True
    assert result["openTurn"] == 3


@pytest.mark.asyncio
async def test_session_load_reports_a_pending_approval(ipc):
    """A crash during the wait leaves the marker behind; the client is told
    which call was being asked about."""
    world, _service, client = ipc
    session = world.sessions.create()
    world.sessions.save(session)
    world.sessions.mark_pending_approval(
        session.session_id,
        {"callId": "c9", "toolName": "bash", "params": {"command": "x"}, "reason": "r"},
    )

    result = client.request("session.load", {"id": session.session_id})

    assert result["pendingApproval"]["toolName"] == "bash"
    assert result["pendingApproval"]["callId"] == "c9"


@pytest.mark.asyncio
async def test_session_load_of_a_clean_session_says_nothing(ipc):
    """The regression guard: an ordinary session must look exactly as before."""
    world, _service, client = ipc
    session = world.sessions.create(title="clean")
    world.sessions.save(session)

    result = client.request("session.load", {"id": session.session_id})

    assert result["recoveredInterrupted"] is False
    assert result["openTurn"] == 0
    assert result["pendingApproval"] == {}


@pytest.mark.asyncio
async def test_session_resume_continues_without_a_new_prompt(ipc):
    """08/Q1: resume carries no text -- the fake records how it was called, so
    the test sees the turn arrived without a prompt behind it."""
    world, _service, client = ipc
    hello = client.request("initialize")
    session_id = hello["sessionId"]
    world.loop.script = [
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="done"))
        ),
    ]
    ack = client.request("session.resume", {"id": session_id})
    assert ack["turnId"] == 1
    assert ack["sessionId"] == session_id
    assert await client.wait_for(lambda: bool(world.loop.calls))
    call = world.loop.calls[-1]
    assert call.get("resumed") is True
    assert "text" not in call
    assert await client.wait_for(client.turn_finished.is_set)
    assert client.events[-1]["data"]["status"] == "completed"
