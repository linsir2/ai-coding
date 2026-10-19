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
from typing import Any

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
        # 11-plan T9 (sanctioned double extension): the boot-resume path hands
        # this loop to PlanRunner, whose policy swap reads loop.policy.
        from ai_coding.core.turn_policy import TurnPolicy

        self.policy = TurnPolicy()

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


class _SavingCloser:
    """The one method ``PlanRunner._persist`` uses on a loop's closer.

    P0-3 (sanctioned double extension): the boot path drives a real PlanRunner
    over the scripted loop, and that runner persists through ``loop.closer`` --
    a stand-in the fake loop never had, which is why a resumed run used to die
    on ``AttributeError`` inside the handler instead of being observed.
    """

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    def save(self, session: Any) -> None:
        self._sessions.save(session)


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
    from ai_coding.core.steering import SteeringQueue

    return SimpleNamespace(
        sessions=SessionService(tmp_path / "sessions"),
        engine=_FakeEngine(),
        # D-1c: the real world builds one and hands it to the sub-agent runner
        # and the loop; the double follows the interface (sanctioned extension).
        notices=SteeringQueue(),
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
    """A command that runs turns (/review, /plan run steps) drives one run.

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
async def test_the_approval_request_says_whether_an_always_can_be_remembered(ipc):
    """P0-4A: an "always" the gate cannot record is a promise it keeps breaking.

    ``grant_key`` is the recorder's own rule (``PermissionGate.grant`` calls it
    and writes nothing when it returns None): bash is excluded by policy, and so
    is any call whose arguments name no single resource.  The answer therefore
    travels with the request -- the panel must not re-implement the rule.
    """
    world, _service, client = ipc
    client.approval_answer = "yes"

    await world.approval.approve("bash", {"command": "ls"}, "warn tier")
    assert client.approval_requests[-1]["grantable"] is False

    await world.approval.approve("write", {"path": "a.py"}, "warn tier")
    assert client.approval_requests[-1]["grantable"] is True


@pytest.mark.asyncio
async def test_a_rejection_can_carry_what_the_operator_would_accept_instead(ipc):
    """C-3: "no" tells the model nothing it can act on.

    The note is the operator *speaking*, so it goes on the user lane -- a
    next-step row in the session's inbox table -- and lands in the very next
    request, right after the refusal it answers.  It is lifted off here, in the
    one place
    that sees both the raw front-end string and the world, which is why the
    shared parser and ``ApprovalResult`` stay exactly as they are (the REPL's
    vocabulary and its pinned call sites included).
    """
    world, _service, client = ipc
    client.request("initialize")
    client.approval_answer = "no::改用 ls -la 看清楚"

    result = await world.approval.approve("bash", {"command": "rm -rf build"}, "warn tier")

    assert result is ApprovalResult.NO
    rows = client.request("session.inbox", {"op": "list"})["rows"]
    assert [(row["text"], row["target"]) for row in rows] == [("改用 ls -la 看清楚", "next-step")]


@pytest.mark.asyncio
async def test_a_bare_answer_still_carries_nothing(ipc):
    """The regression half: an answer with no note must leave the user lane
    untouched, or every refusal would inject a phantom instruction."""
    world, _service, client = ipc
    client.request("initialize")
    client.approval_answer = "always"

    result = await world.approval.approve("write", {"path": "a.py"}, "warn tier")

    assert result is ApprovalResult.ALWAYS
    assert client.request("session.inbox", {"op": "list"})["rows"] == []


@pytest.mark.asyncio
async def test_a_note_the_queue_refuses_is_logged_not_rewritten(ipc, caplog):
    """The queue refuses a line starting with ``/`` (that is a command). The
    operator's words are not ours to rewrite, so the note is dropped with a log
    line rather than silently mangled into something else."""
    import logging

    world, _service, client = ipc
    client.request("initialize")
    client.approval_answer = "no::/usr/bin/rm -rf build"
    caplog.set_level(logging.INFO, logger="ai_coding.server.handlers")

    result = await world.approval.approve("bash", {"command": "rm -rf build"}, "warn tier")

    assert result is ApprovalResult.NO
    assert client.request("session.inbox", {"op": "list"})["rows"] == []
    assert "looks like a command" in caplog.text


@pytest.mark.asyncio
async def test_the_mcp_answer_still_declares_itself_rememberable(ipc):
    """A remote tool is remembered by name (``grant_key`` splits on ``__``), so
    the M2 shape stays an offer rather than a silent once."""
    world, _service, client = ipc
    client.approval_answer = "yes"

    await world.approval.approve("fs__write_file", {"server": "fs"}, "warn tier")

    assert client.approval_requests[-1]["grantable"] is True


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
        # P0-4A migration: the payload gained the gate's own grantable answer
        # (sanctioned -- the round trip and its vocabulary are what this pins).
        "grantable": True,
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
    # 19 §3.2: detail/intent are always present (empty = a plain question).
    assert client.ask_requests[-1] == {
        "question": "哪个文件？",
        "options": ["a.py", "b.py"],
        "detail": "",
        "intent": "",
    }


@pytest.mark.asyncio
async def test_user_prompt_carries_detail_and_intent_over_the_wire(ipc):
    """19 §3.2: the plan review rides the same ask; plain questions ignore the keys."""
    from ai_coding.security.user_prompt import UserAnswer

    world, _service, client = ipc
    client.ask_answer = '{"choice":"approve"}'
    answer = await world.user_prompt.ask(
        "批准这个计划并开始执行？",
        ["开始执行", "继续规划"],
        detail="# 计划\n正文",
        intent="plan_review",
    )
    assert answer == UserAnswer(answered=True, text='{"choice":"approve"}')
    assert client.ask_requests[-1] == {
        "question": "批准这个计划并开始执行？",
        "options": ["开始执行", "继续规划"],
        "detail": "# 计划\n正文",
        "intent": "plan_review",
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
    result = client.request("session.stats")
    created = result.pop("createdTime")  # a live ISO stamp; equality below
    assert created
    assert result == {
        "tokens": 0,
        "maxContextTokens": 48000,
        "mode": "normal",
        "readonly": False,
        "turnCount": 0,
        "budget": None,  # no recorder in this world: tracing is off
    }


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
    result = client.request("session.stats")
    created = result.pop("createdTime")
    assert created
    assert result == {
        "tokens": 1234,
        "maxContextTokens": 48000,
        "mode": "normal",
        "readonly": False,
        "turnCount": 0,
        "budget": None,
    }


@pytest.mark.asyncio
async def test_session_stats_carry_turn_count_and_created_time(ipc):
    """The sidebar's session card reads facts the transcript never shows."""
    world, _service, client = ipc
    session = world.sessions.create()
    session.turn_count = 7
    session.created_time = "2026-01-01T10:00:00Z"
    world.sessions.save(session)
    client.request("session.load", {"id": session.session_id})

    result = client.request("session.stats")
    assert result["turnCount"] == 7
    assert result["createdTime"] == "2026-01-01T10:00:00Z"


@pytest.mark.asyncio
async def test_session_stats_carry_the_budget_when_a_summary_exists(ipc, tmp_path):
    """Same numbers /budget renders -- one cost formula, two consumers."""
    import json as _json

    from ai_coding.core.budget import Pricing

    world, _service, client = ipc
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(
        _json.dumps(
            {"prompt_tokens": 2000, "completion_tokens": 500, "cached_tokens": 1000}
        ),
        encoding="utf-8",
    )

    class _Store:
        def summary_path(self, session_id):
            return summary_file

        def read_summary(self, session_id):
            return _json.loads(summary_file.read_text(encoding="utf-8"))

    class _Recorder:
        store = _Store()
        pricing = Pricing(input_per_1k=1.0, output_per_1k=2.0, cached_per_1k=0.1)
        pricings = None

    world.loop.trace = _Recorder()
    client.request("initialize")

    budget = client.request("session.stats")["budget"]
    assert budget["tokens"] == 2500
    assert abs(budget["cost"] - 2.1) < 1e-9
    assert budget["cachedPercent"] == 50


@pytest.mark.asyncio
async def test_session_stats_reports_the_posture(ipc):
    """P5 (18 §10.5): both posture axes ride the stats payload, always."""
    world, _service, client = ipc
    session = world.sessions.create()
    session.mode, session.readonly = "plan", True
    world.sessions.save(session)
    client.request("session.load", {"id": session.session_id})

    result = client.request("session.stats")
    created = result.pop("createdTime")
    assert created
    assert result == {
        "tokens": 0,
        "maxContextTokens": 48000,
        "mode": "plan",
        "readonly": True,
        "turnCount": 0,
        "budget": None,
    }


@pytest.mark.asyncio
async def test_session_load_reports_the_posture(ipc):
    """P5 (18 §10.5): the load payload carries the same two keys."""
    world, _service, client = ipc
    session = world.sessions.create()
    session.mode, session.readonly = "plan", True
    world.sessions.save(session)

    result = client.request("session.load", {"id": session.session_id})

    assert (result["mode"], result["readonly"]) == ("plan", True)

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


@pytest.mark.asyncio
async def test_interrupt_disarms_an_armed_task(ipc):
    """11-spec §8: Esc stops the whole armed run, not only the round."""
    world, _service, client = ipc
    created = client.request("session.create", {"title": "armed"})
    session_id = created["id"]
    from ai_coding.domain.task import TaskState

    session = world.sessions.load(session_id)
    task = TaskState(goal="g")
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    session.task = task
    world.sessions.save(session)
    client.request("session.load", {"id": session_id})  # live object = the armed one

    client.request("session.interrupt")

    reloaded = world.sessions.load(session_id)
    assert reloaded.task.armed is False


@pytest.mark.asyncio
async def test_initialize_consumes_an_armed_session_once(ipc):
    """11-spec §7.2: the boot candidate is loaded as live and resumed; the
    second initialize goes back to a fresh session."""
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    armed = world.sessions.create(title="armed")
    task = TaskState(goal="g")
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    armed.task = task
    world.sessions.save(armed)
    world.pending_resume = world.sessions.load(armed.session_id)

    hello = client.request("initialize")

    assert hello["sessionId"] == armed.session_id
    # 可达+有痕批次: the response tells the shell to open the session and
    # carries the one note the operator must see (race-free, unlike a
    # console.line emitted while the route is still Home).
    assert hello.get("resumed") is True
    assert "自动续跑" in str(hello.get("resumeNote") or "")
    assert world.pending_resume is None
    again = client.request("initialize")
    assert again["sessionId"] != armed.session_id
    assert again.get("resumed") is False
    assert not again.get("resumeNote")


@pytest.mark.asyncio
async def test_initialize_resets_a_plan_posture_before_resuming(ipc):
    """R07: the approval that armed a plan can be followed by a crash; boot
    then resumes execution, but the session kept its PLAN posture -- the
    status bar said "PLAN\u00b7readonly" while the runner executed (F-46).
    Boot sends the parent back to build first, and persists it before the
    run so a crash mid-resume still reads build."""
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    armed = world.sessions.create(title="armed")
    task = TaskState(goal="g")
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    armed.task = task
    armed.mode = "plan"
    armed.readonly = True
    armed.pending_posture = {"mode": "normal", "readonly": False}
    world.sessions.save(armed)
    world.pending_resume = world.sessions.load(armed.session_id)

    hello = client.request("initialize")

    assert hello["sessionId"] == armed.session_id
    reloaded = world.sessions.load(armed.session_id)
    assert reloaded.mode == "normal"
    assert reloaded.readonly is False
    assert reloaded.pending_posture is None


@pytest.mark.asyncio
async def test_boot_resume_steps_are_steps_and_the_run_ends_once(ipc):
    """P0-3: a resumed run owes the client the same shape a command run does.

    ``command_run`` is what tells the bridge that the loop's ``TurnFinished`` is
    an inner *step* rather than the end of the run; the boot path never set it,
    so each plan step arrived as ``turn.finished`` -- which released ``busy``,
    drew a red turn-end marker and dispatched one queued line per step.  It also
    emitted no terminal event of its own (the shared ``_launch_turn`` wrapper
    only reports unhandled exceptions), so nothing ever closed the run.
    """
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    armed = world.sessions.create(title="armed")
    task = TaskState(goal="g")
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    armed.task = task
    armed.open_turn = 1
    world.sessions.save(armed)
    world.pending_resume = world.sessions.load(armed.session_id)

    # One script entry per turn: the fake replays the whole script on every
    # call, and the resumed round plus the plan step are two turns.
    world.loop.script = [
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="step done"))
        )
    ]
    world.loop.closer = _SavingCloser(world.sessions)

    hello = client.request("initialize")

    assert hello["sessionId"] == armed.session_id
    # The shell has to know a run is actually starting: the breaker branch also
    # answers `resumed`, and it starts nothing (nothing would release `busy`).
    assert hello.get("resuming") is True
    assert await client.wait_for(client.turn_finished.is_set)

    types = [event["type"] for event in client.events]
    assert types.count("turn.step") == 2, types
    assert types.count("turn.finished") == 1, types
    # 2026-10-17: the wall the receipt used to die on.  The runner's observer
    # here is the REAL bridge (on_event, no __call__); the runner tests passed
    # a bare callable, so the two layers were each green while the wiring
    # between them emitted nothing.  One real step == one receipt, counted
    # here through the whole chain.
    assert types.count("plan.receipt") == 1, types
    assert types[-1] == "turn.finished"
    assert client.events[-1]["data"]["status"] == "completed"
    # The task disarmed itself when the run finished (PlanRunner's finally).
    assert world.sessions.load(armed.session_id).task.armed is False


@pytest.mark.asyncio
async def test_a_breaker_resume_starts_no_run(ipc):
    """The other branch of the same field: past the cap the task is disarmed and
    **no** run begins, so the shell must not sit busy over a run that will never
    announce an end."""
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    armed = world.sessions.create(title="flapping")
    task = TaskState(goal="g")
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    task.resume_count = 5
    armed.task = task
    world.sessions.save(armed)
    world.pending_resume = world.sessions.load(armed.session_id)

    hello = client.request("initialize")

    assert hello.get("resumed") is True
    assert hello.get("resuming") is False
    assert world.sessions.load(armed.session_id).task.armed is False


@pytest.mark.asyncio
async def test_a_plain_boot_resumes_nothing(ipc):
    """No armed task: the shell lands on Home and the field stays false, so the
    busy flag is never set for a session that is not running."""
    world, _service, client = ipc

    hello = client.request("initialize")

    assert hello.get("resumed") is False
    assert hello.get("resuming") is False


@pytest.mark.asyncio
async def test_boot_resume_breaker_disarms_past_the_cap(ipc):
    """11-spec §7.2 + audit fix: past the cap the RELOADED task is disarmed
    (the old sketch mutated the pre-load object and the disk copy stayed
    armed) with a notice."""
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    armed = world.sessions.create(title="flapping")
    task = TaskState(goal="g")
    task.add("step")
    task.armed = True
    task.armed_profile = "plan"
    task.resume_count = 5
    armed.task = task
    world.sessions.save(armed)
    world.pending_resume = world.sessions.load(armed.session_id)

    hello = client.request("initialize")

    assert hello["sessionId"] == armed.session_id
    # The breaker's notice rides the same response field (guaranteed visible
    # before the session route mounts).
    assert hello.get("resumed") is True
    assert "上限" in str(hello.get("resumeNote") or "")
    reloaded = world.sessions.load(armed.session_id)
    assert reloaded.task.armed is False


@pytest.mark.asyncio
# ---- D-1b: the panel's snapshot --------------------------------------------


@pytest.mark.asyncio
async def test_session_jobs_reports_the_workspace_jobs(ipc, tmp_path):
    """The panel needs a snapshot for the client that connected after a job
    started; job.changed only carries what happens from here on."""
    _world, _service, client = ipc
    from ai_coding.core.jobs import JobRegistry

    workspace = getattr(_world, "workspace", None) or str(tmp_path)
    registry = JobRegistry(root=Path(workspace) / "jobs")
    job = registry.start("sleep 30", cwd=workspace, session_id="s", emit=lambda e: None)
    _world.jobs = registry
    try:
        client.request("initialize")
        response = client.request("session.jobs")

        assert response["available"] is True
        assert [row["id"] for row in response["jobs"]] == [job.id]
        assert response["jobs"][0]["status"] == "running"
        assert "sleep 30" in response["jobs"][0]["command"]
    finally:
        registry.kill(job.id, reason="test over")


@pytest.mark.asyncio
async def test_session_jobs_on_a_world_without_jobs_says_it_is_unavailable(ipc):
    """Absence of a registry is not the same as an empty list, and the frontend
    casts on this key."""
    _world, _service, client = ipc
    _world.jobs = None

    client.request("initialize")

    assert client.request("session.jobs") == {"jobs": [], "available": False}


# ---- D-1c: a finished job wakes an idle session, three times at most --------
#
# dsh delivers a completion as `wakeup` by default (its `quiet` opt-out) and oc's
# `sessions.synthetic` resumes unless told not to; ours used to wait for the next
# human message. The budget is the part both harnesses needed: a woken turn can
# start the job whose completion wakes it again, and without a ceiling that chain
# spends money while nobody is watching. dsh leaves it uncapped by default; we cap
# at three, which is the conservative reading of the same design.


async def _settle_a_job(world, tmp_path):
    """A job that finishes for real, as the notice path sees it."""
    from ai_coding.core.jobs import JobRegistry

    registry = JobRegistry(root=tmp_path / "jobs", notices=world.notices)
    _world_jobs = registry
    world.jobs = registry
    job = registry.start("exit 0", cwd=str(tmp_path), session_id="s", emit=lambda e: None)
    for _ in range(100):
        if job.status != "running":
            break
        await asyncio.sleep(0.05)
    return registry, job


@pytest.mark.asyncio
async def test_a_finished_job_wakes_an_idle_session(ipc, tmp_path):
    _world, service, client = ipc
    client.request("initialize")
    world_loop = _world.loop
    before = len(world_loop.calls)

    await _settle_a_job(_world, tmp_path)
    for _ in range(100):  # the wake rides call_soon_threadsafe
        if len(world_loop.calls) > before:
            break
        await asyncio.sleep(0.05)

    assert len(world_loop.calls) == before + 1, "an idle session must be woken"
    assert "notice" in str(world_loop.calls[-1]["text"])


@pytest.mark.asyncio
async def test_the_wake_turn_reaches_the_wire_like_any_other_turn(ipc, tmp_path):
    """2026-10-18: ``_wake_turn`` called ``process_input(session, text)`` with
    no observer, and with tracing on ``_bind_observer(None)`` recomposed the
    braid off the construction-time base (Null) -- the bridge was dropped, so
    the wake turn streamed to nobody and every background event until the next
    human turn went nowhere the operator could see.  A wake is a turn like any
    other: it hands the bridge to both channels."""
    _world, _service, client = ipc
    client.request("initialize")
    world_loop = _world.loop
    before = len(world_loop.calls)

    await _settle_a_job(_world, tmp_path)
    for _ in range(100):
        if len(world_loop.calls) > before:
            break
        await asyncio.sleep(0.05)

    wake = world_loop.calls[-1]
    assert "notice" in str(wake["text"]), "the extra call is the wake turn"
    assert wake["observer"] is not None, "the wake turn must carry the bridge"
    assert wake["on_delta"] is not None, "and the streaming channel with it"


@pytest.mark.asyncio
def test_the_wake_decision_refuses_when_a_turn_is_already_running() -> None:
    """The guard, as a decision (the key path in this harness cannot hold a turn
    open -- the fake loop never yields -- so the integration test above covers
    the wake itself and this covers the refusals)."""
    from ai_coding.server.handlers import wake_decision

    assert wake_decision(budget_left=3, turns_in_flight=0) is True
    assert wake_decision(budget_left=3, turns_in_flight=1) is False, (
        "a turn in flight drains the notice itself"
    )
    assert wake_decision(budget_left=0, turns_in_flight=0) is False, "the budget must hold"


@pytest.mark.asyncio
async def test_the_wake_budget_runs_out_after_three_and_a_user_turn_resets_it(ipc, tmp_path):
    """dsh's rule: the budget counts turns opened since the human last spoke."""
    from ai_coding.server.handlers import WAKE_BUDGET

    _world, service, client = ipc
    client.request("initialize")
    world_loop = _world.loop
    # The first completion spends one of the budget's three.
    registry, _job = await _settle_a_job(_world, tmp_path)
    await asyncio.sleep(0.3)

    # Keep finishing jobs until the budget is spent: three wakes in total, then
    # silence. (The count is total-since-the-human-spoke, not "three more".)
    for _ in range(WAKE_BUDGET + 2):
        job = registry.start("exit 0", cwd=str(tmp_path), session_id="s", emit=lambda e: None)
        for _ in range(100):
            if job.status != "running":
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        registry.discard(job.id)
    # Let the count settle before reading it: the wake rides a thread hop, and a
    # loaded machine can deliver it a beat late.  Stability, not a fixed sleep,
    # is what makes the assertion mean "the budget ended", not "the box was slow".
    capped = len(world_loop.calls)
    for _ in range(20):
        await asyncio.sleep(0.1)
        if len(world_loop.calls) == capped:
            break
        capped = len(world_loop.calls)
    assert capped == WAKE_BUDGET, f"expected {WAKE_BUDGET} wakes in total, got {capped}"

    # One more completion: quiet now.
    job = registry.start("exit 0", cwd=str(tmp_path), session_id="s", emit=lambda e: None)
    for _ in range(100):
        if job.status != "running":
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    assert len(world_loop.calls) == capped, "the budget must hold"

    # The human speaks: the budget refills.
    client.request("session.prompt", {"text": "hello"})
    await asyncio.sleep(0.3)
    job = registry.start("exit 0", cwd=str(tmp_path), session_id="s", emit=lambda e: None)
    for _ in range(100):
        if job.status != "running":
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    assert len(world_loop.calls) > capped + 1, "a user turn must refill the budget"


# ---- 2026-10-17: a refused wake is latched and replayed at the runway -------
#
# Live-found gap: a job settling during a turn's *tail* -- after its last
# iteration boundary (the real tail includes the multi-second memory harvest)
# -- had its wake refused for "turn in flight" and then dropped: the notice sat
# in the queue until the next human message.  dsh latches a wake it cannot
# deliver and replays it at convergence, with a hasPending guard; this is that
# shape at the server layer, where the budget lives.


def _park_loop(world):
    """Make the fake loop's next turns park inside process_input.

    The call is recorded by the fake only after release, so the call list says
    "this turn finished"; ``entered`` proves the turn is in flight.
    """
    original = world.loop.process_input
    entered, release = asyncio.Event(), asyncio.Event()

    async def park(session, text, on_delta=None, observer=None):
        entered.set()
        await release.wait()
        return await original(session, text, on_delta=on_delta, observer=observer)

    world.loop.process_input = park
    return entered, release


async def _wait_until(check, attempts: int = 100) -> bool:
    for _ in range(attempts):
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


@pytest.mark.asyncio
async def test_a_wake_refused_mid_turn_is_latched_and_replayed_at_the_runway(ipc):
    """The bug this fixes: the wake is refused because a turn is in flight, but
    that turn may be past its last drain boundary -- the notice belongs to the
    *next* runway, not to a dropped call."""
    world, _service, client = ipc
    client.request("initialize")
    entered, release = _park_loop(world)

    client.request("session.prompt", {"text": "长回合"})
    assert await _wait_until(entered.is_set), "precondition: the turn is in flight"

    # The job settles now.  Refused (correctly), and latched instead of lost.
    world.notices.put("[notice] job done", wake=True)
    await asyncio.sleep(0.3)
    assert "长回合" not in [c["text"] for c in world.loop.calls], (
        "precondition: the parked turn has not recorded yet"
    )

    release.set()
    assert await _wait_until(lambda: len(world.loop.calls) >= 2), (
        "the latched wake must open a turn once the runway clears"
    )
    texts = [c["text"] for c in world.loop.calls]
    assert texts[0] == "长回合"
    assert "notice" in texts[1], f"the replay keeps the wake prompt: {texts}"


@pytest.mark.asyncio
async def test_a_latched_wake_is_suppressed_when_the_queue_was_already_drained(ipc):
    """dsh's hasPending guard: a latched replay is suppressed when the queue no
    longer holds the wake -- the dying turn's own boundary drained it, and a
    replay would open a turn with nothing to look at."""
    world, _service, client = ipc
    client.request("initialize")
    entered, release = _park_loop(world)

    client.request("session.prompt", {"text": "长回合"})
    assert await _wait_until(entered.is_set)

    world.notices.put("[notice] job done", wake=True)  # refused -> latched
    await asyncio.sleep(0.3)
    assert world.notices.drain() == ["[notice] job done"], (
        "simulate the in-flight turn's own iteration boundary draining it"
    )

    release.set()
    await asyncio.sleep(0.5)
    texts = [c["text"] for c in world.loop.calls]
    assert texts == ["长回合"], f"no spurious wake for an empty queue: {texts}"


@pytest.mark.asyncio
async def test_an_interrupt_drops_a_latched_wake(ipc):
    """Esc stops everything, a pending replay included (dsh clears the latch on
    cancel): the notice stays durable in the queue and rides the next human
    turn -- what Esc means, nothing more."""
    world, _service, client = ipc
    client.request("initialize")
    entered, _release = _park_loop(world)

    client.request("session.prompt", {"text": "长回合"})
    assert await _wait_until(entered.is_set)
    world.notices.put("[notice] job done", wake=True)  # refused -> latched
    await asyncio.sleep(0.3)

    client.request("session.interrupt")
    await asyncio.sleep(0.5)
    assert world.loop.calls == [], "the parked turn was cancelled before recording"
    assert world.notices.pending == 1, "the notice itself is durable, Esc or not"


# ---- D-2 Phase 1: the inbox table -------------------------------------------
#
# dsh keeps one event-sourced inbox with next-turn/next-step targets and oc one
# SQLite table with steer/queue deliveries; we had two in-memory queues that
# agreed on nothing and survived nothing.  The table lives on the session so
# restart survival comes from the session file, under the same rule every other
# key follows: absent when empty.


def _inbox(ipc_client, op: str, **params):
    return ipc_client.request("session.inbox", {"op": op, **params})


@pytest.mark.asyncio
async def test_inbox_add_returns_an_id_and_lists_it_back(ipc):
    _world, _service, client = ipc
    client.request("initialize")

    added = _inbox(client, "add", text="先查日志", target="next-turn")
    rows = _inbox(client, "list")["rows"]

    assert added["ok"] is True and added["id"]
    assert [row["id"] for row in rows] == [added["id"]]
    assert rows[0]["text"] == "先查日志" and rows[0]["target"] == "next-turn"


@pytest.mark.asyncio
async def test_inbox_rows_survive_a_reload(ipc, tmp_path):
    """The whole point of putting the table on the session: what the operator
    queued is still there after a restart."""
    _world, _service, client = ipc
    client.request("initialize")
    added = _inbox(client, "add", text="重启后还在吗")

    reloaded = _world.sessions.load(_world.sessions.list_sessions()[0].session_id)

    assert [row.text for row in reloaded.inbox] == ["重启后还在吗"]
    assert reloaded.inbox[0].id == added["id"]


@pytest.mark.asyncio
async def test_inbox_edit_changes_text_and_keeps_the_id(ipc):
    _world, _service, client = ipc
    client.request("initialize")
    added = _inbox(client, "add", text="错别字在这里")

    edited = _inbox(client, "edit", id=added["id"], text="改对了")

    assert edited["ok"] is True
    assert _inbox(client, "list")["rows"][0]["text"] == "改对了"
    assert _inbox(client, "list")["rows"][0]["id"] == added["id"]


@pytest.mark.asyncio
async def test_inbox_delete_removes_the_row(ipc):
    _world, _service, client = ipc
    client.request("initialize")
    added = _inbox(client, "add", text="不要了")

    assert _inbox(client, "delete", id=added["id"])["ok"] is True
    assert _inbox(client, "list")["rows"] == []


@pytest.mark.asyncio
async def test_inbox_refuses_blank_text_and_unknown_ids(ipc):
    _world, _service, client = ipc
    client.request("initialize")

    assert _inbox(client, "add", text="   ")["ok"] is False
    assert _inbox(client, "edit", id="nope", text="x")["ok"] is False
    assert _inbox(client, "delete", id="nope")["ok"] is False


# ---- D-2 Phase 2: the backend reads the same table --------------------------
#
# dsh's loop claims next-turn rows for a turn's first step and next-step rows
# after that (agent.ts:313/366); oc's nextPromotable takes a steer row before a
# queued one, and a steer-scoped drain never promotes queued rows
# (execution.ts:157-159).  Ours was two mechanisms: the composer's steer line
# went to an in-memory FIFO while the panel wrote a table.  Now both write the
# table, and the loop drains next-step rows from it.


def _inbox_rows(ipc_client):
    return ipc_client.request("session.inbox", {"op": "list"})["rows"]


@pytest.mark.asyncio
async def test_session_steer_writes_a_next_step_row_and_refuses_commands(ipc):
    """Same contract as the old FIFO endpoint -- text accepted, '/' and blanks
    refused, pending counts what is waiting -- but the row lands in the session
    table where the panel and a restart can see it."""
    _world, _service, client = ipc
    client.request("initialize")

    ok = client.request("session.steer", {"text": "改个方向：先看测试"})
    assert ok["accepted"] is True and ok["pending"] == 1

    refused = client.request("session.steer", {"text": "/help"})
    assert refused["accepted"] is False and refused["pending"] == 1

    empty = client.request("session.steer", {"text": "   "})
    assert empty["accepted"] is False and empty["pending"] == 1

    rows = _inbox_rows(client)
    assert len(rows) == 1 and rows[0]["target"] == "next-step"
    assert rows[0]["text"] == "改个方向：先看测试"


@pytest.mark.asyncio
async def test_inbox_promote_flips_a_row_to_next_step_and_persists(ipc):
    """oc's projectDeliveryChanged in miniature: only the target moves, never
    the text.  Interrupting the running turn to honor it is Phase 3; this step
    only records the intent, durably."""
    _world, _service, client = ipc
    client.request("initialize")
    added = client.request("session.inbox", {"op": "add", "text": "等下再说"})

    promoted = client.request("session.inbox", {"op": "promote", "id": added["id"]})

    assert promoted["ok"] is True
    rows = _inbox_rows(client)
    assert rows[0]["target"] == "next-step" and rows[0]["text"] == "等下再说"
    assert rows[0]["id"] == added["id"]


@pytest.mark.asyncio
async def test_inbox_promote_of_an_unknown_id_says_so(ipc):
    _world, _service, client = ipc
    client.request("initialize")

    result = client.request("session.inbox", {"op": "promote", "id": "nope"})

    assert result["ok"] is False


# ---- D-2 Phase 3: promote interrupts and wakes --------------------------------
#
# dsh's steer() always wakes and oc resumes on interrupt; ours kills the turn
# outright (CancelledError -> _close_turn), so a promote that did not chain a
# wake would leave its own row waiting for the next human message -- the exact
# opposite of what the operator pressed the button for.  The chain shares the
# D-1c budget: a wake is a wake no matter who asked for it.


async def _add_row(client, text: str = "先看日志", target: str = "next-turn") -> str:
    added = client.request("session.inbox", {"op": "add", "text": text, "target": target})
    assert added["ok"] is True
    return added["id"]


@pytest.mark.asyncio
async def test_promote_while_idle_flips_and_wakes(ipc, tmp_path):
    """dsh's steer() always wakes: the operator pressed promote, which is
    consent to spend a turn.  No turn is running, so nothing is interrupted."""
    _world, _service, client = ipc
    client.request("initialize")
    world_loop = _world.loop
    row_id = await _add_row(client)
    before = len(world_loop.calls)

    result = client.request("session.inbox", {"op": "promote", "id": row_id})

    assert result == {"ok": True, "id": row_id, "interrupted": 0, "woke": True}
    for _ in range(100):
        if len(world_loop.calls) > before:
            break
        await asyncio.sleep(0.05)
    assert len(world_loop.calls) == before + 1, "an idle promote must open a turn"
    assert "notice" in str(world_loop.calls[-1]["text"]).lower() or "steer" in str(
        world_loop.calls[-1]["text"]
    ).lower() or len(world_loop.calls[-1]["text"]) > 0


@pytest.mark.asyncio
async def test_promote_while_running_interrupts_then_wakes_after_settle(ipc, tmp_path):
    """The full chain: the running turn dies (aborted, like Esc), and only once
    it has settled does the wake turn open -- overlapping turns would read and
    write the same session object interleaved."""
    _world, _service, client = ipc
    client.request("initialize")
    _world.loop = _SlowLoop()
    client.request("session.prompt", {"text": "slow"})
    assert await client.wait_for(lambda: bool(_world.loop.calls))
    row_id = await _add_row(client, text="停下，先看这个")
    before = len(_world.loop.calls)

    result = client.request("session.inbox", {"op": "promote", "id": row_id})

    assert result["ok"] is True and result["interrupted"] == 1
    assert await client.wait_for(client.turn_finished.is_set)
    for _ in range(200):
        if len(_world.loop.calls) > before:
            break
        await asyncio.sleep(0.05)
    assert len(_world.loop.calls) == before + 1, "the wake must wait for the settle"
    rows = client.request("session.inbox", {"op": "list"})["rows"]
    assert [row["target"] for row in rows if row["id"] == row_id] == ["next-step"]


@pytest.mark.asyncio
async def test_promote_with_no_budget_left_only_flips(ipc, tmp_path):
    """The budget is shared with job wakes (D-1c): a wake is a wake.  When it is
    spent, promote degrades to a flip and says so -- silently dropping the row
    would be worse, and waking anyway would uncap the meter."""
    _world, _service, client = ipc
    client.request("initialize")
    # Spend the shared budget for real: three promotes, three wakes.
    for _ in range(3):
        row_id = await _add_row(client)
        assert client.request("session.inbox", {"op": "promote", "id": row_id})["woke"] is True
    row_id = await _add_row(client, text="预算用完了")

    result = client.request("session.inbox", {"op": "promote", "id": row_id})

    assert result["ok"] is True and result["woke"] is False
    rows = client.request("session.inbox", {"op": "list"})["rows"]
    assert [row["target"] for row in rows if row["id"] == row_id] == ["next-step"]


@pytest.mark.asyncio
async def test_an_armed_goal_is_driven_after_the_turn_that_armed_it(ipc):
    """D-4: ``start_goal`` arms mid-turn; the run starts at that turn's boundary.

    The plan's approval path dispatches ``/plan run`` from the frontend (a UI
    event); a model's own arming has no UI event, so the dispatch lives in the
    prompt turn's wrapper.  Pinned here: the goal round opens on the *main*
    session as a ``[goal] 第 1/…`` turn, the whole run is one envelope (inner
    turn -> ``turn.step``, exactly one terminal ``turn.finished``), and the
    run disarms on its way out."""
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    world.loop.closer = _SavingCloser(world.sessions)
    session_id = client.request("session.create", {"title": "goal"})["id"]
    # Arm on disk first, then load: the live object is the loaded one (the
    # same order test_interrupt_disarms_an_armed_task uses).
    session = world.sessions.load(session_id)
    session.task = TaskState(goal="迁移 X")
    session.task.armed = True
    session.task.armed_profile = "goal"
    world.sessions.save(session)
    client.request("session.load", {"id": session_id})

    def finish(observer, _delta) -> None:
        observer.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="done"))
        )

    world.loop.script = [finish]
    client.request("session.prompt", {"text": "把 jest 迁到 vitest", "id": session_id})

    assert await client.wait_for(
        lambda: any(c["text"].startswith("[goal] 第 1/") for c in world.loop.calls)
    )
    assert await client.wait_for(client.turn_finished.is_set)
    texts = [call["text"] for call in world.loop.calls]
    assert texts[0] == "把 jest 迁到 vitest"
    assert texts[1].startswith("[goal] 第 1/20 轮")
    assert "迁移 X" in texts[1]
    # Every exit disarms; a finished goal with no items and no phase claim
    # evaporates from the file (the bare-goal noise rule) -- read it defensively.
    live = world.sessions.load(session_id)
    assert getattr(live.task, "armed", False) is False


@pytest.mark.asyncio
async def test_boot_resume_drives_a_goal_task_and_the_note_names_goal(ipc):
    """D-4: the boot scan recognises a ``goal``-armed task; the note says what
    was interrupted, the GoalRunner opens its round, and the breaker field is
    the same one the plan path uses."""
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    armed = world.sessions.create(title="goal")
    task = TaskState(goal="迁移 X")
    task.armed = True
    task.armed_profile = "goal"
    armed.task = task
    world.sessions.save(armed)
    world.pending_resume = world.sessions.load(armed.session_id)
    world.loop.script = [
        lambda obs, _delta: obs.on_event(
            TurnFinished(outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="done"))
        )
    ]
    world.loop.closer = _SavingCloser(world.sessions)

    hello = client.request("initialize")

    assert hello["sessionId"] == armed.session_id
    assert hello.get("resumed") is True and hello.get("resuming") is True
    note = str(hello.get("resumeNote") or "")
    assert "[goal]" in note and "目标任务" in note
    assert await client.wait_for(
        lambda: any(c["text"].startswith("[goal] 第 1/") for c in world.loop.calls)
    )
    assert await client.wait_for(client.turn_finished.is_set)
    assert await client.wait_for(
        lambda: getattr(world.sessions.load(armed.session_id).task, "armed", False)
        is False
    )
    # The itemless episode evaporates from the file once it stops (no items,
    # no armed, no phase claim -> the bare-goal noise rule); its trace is the
    # resume note and the round prompts in the transcript, by design.
    assert world.sessions.load(armed.session_id).task is None


@pytest.mark.asyncio
async def test_a_goal_breaker_resume_names_the_goal_and_starts_no_run(ipc):
    """D-4: the breaker branch is shared with the plan path; what differs for
    goals is the note (it must name the thing it paused) -- and, like the plan
    path, it must start nothing (a shell waiting on a run that never comes
    would sit busy forever)."""
    world, _service, client = ipc
    from ai_coding.domain.task import TaskState

    armed = world.sessions.create(title="goal")
    task = TaskState(goal="迁移 X")
    task.armed = True
    task.armed_profile = "goal"
    task.resume_count = 5
    armed.task = task
    world.sessions.save(armed)
    world.pending_resume = world.sessions.load(armed.session_id)

    hello = client.request("initialize")

    assert hello.get("resumed") is True and hello.get("resuming") is False
    note = str(hello.get("resumeNote") or "")
    assert "[goal]" in note and "目标任务" in note and "上限" in note
    assert world.loop.calls == []  # nothing started
    # The breaker disarmed it; an itemless episode then evaporates by design.
    assert getattr(world.sessions.load(armed.session_id).task, "armed", False) is False
