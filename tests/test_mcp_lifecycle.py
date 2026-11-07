"""M3: the lifecycle — one supervisor per server, non-blocking startup, and a
retry loop that keeps the "it came back by itself" promise.

Non-blocking startup (OpenCode's fork, and the operator's choice) means
``open_all`` only starts the supervisors and returns: a row is ``connecting``
until its first attempt settles, so every assertion here waits for that itself
rather than assuming a synchronous connect.

Two experiments shaped this file.  A stdio server that exits after tools/list
leaves the parked owner task completely unaware (it sits in ``_stop.wait()``,
never reading the stream), so an *idle* death is invisible and the only honest
signal is the next failed call — which is why a transport-class failure marks
the server dead and why an ordinary tool error does not.  And ``_serve`` never
ends after it succeeds, so nothing here waits on the owner's task.

Instrumentation lives on the *factory*, not the manager: observing retries is a
test's need, and a production API only tests consume is exactly what M1 deleted.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import ai_coding.core.mcp as mcp_module
from ai_coding.config.models import MCPConfig, MCPServerConfig, MCPTimeout
from ai_coding.core.events import Degraded, RecordingObserver
from ai_coding.core.mcp import MCPManager
from ai_coding.tools.mcp_call import MCPCallTool

pytestmark = pytest.mark.asyncio


# ---- helpers ---------------------------------------------------------------


def _tool(name="read_file"):
    return SimpleNamespace(name=name, description="reads", input_schema={"type": "object"})


class _Scripted:
    """A session whose ``call_tool`` raises what the script says."""

    def __init__(self, raise_on_call: BaseException | None = None) -> None:
        self._raise_on_call = raise_on_call
        self.calls = 0

    async def list_tools(self):
        return SimpleNamespace(tools=[_tool("read_file")])

    async def call_tool(self, name, arguments=None, progress_callback=None):
        self.calls += 1
        if self._raise_on_call is not None:
            raise self._raise_on_call
        return SimpleNamespace(
            content=[SimpleNamespace(text=f"ok:{name}")],
            structured_content=None,
            is_error=False,
        )


def _session() -> _Scripted:
    return _Scripted()


def _ok_factory(session: _Scripted | None = None):
    item = session or _session()

    def factory(server):
        @asynccontextmanager
        async def _cm():
            yield item

        return _cm()

    return factory


async def _until(predicate, timeout: float = 5.0, step: float = 0.005) -> bool:
    """Wait for a condition the background supervisors will eventually make true.

    Lives in the test because production has no such need: the TUI starts
    immediately and reads rows as they settle, and nothing waits on MCP.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _cfg(*servers: MCPServerConfig) -> MCPConfig:
    return MCPConfig(enabled=True, servers=list(servers))


def _server(name: str, **kwargs) -> MCPServerConfig:
    kwargs.setdefault("timeout", MCPTimeout(startup=200, catalog=200))
    return MCPServerConfig(name=name, command=["x"], **kwargs)


def _statuses(manager: MCPManager) -> dict[str, str]:
    return {row["name"]: row["status"] for row in manager.servers()}


def _rows(manager: MCPManager) -> dict[str, dict]:
    return {row["name"]: row for row in manager.servers()}


class _Factory:
    """A session_factory that scripts what each attempt does.

    The only instrumentation the tests need: how many attempts ran, when each
    started, and how many connected at once.
    """

    def __init__(self, script, delay: float = 0.05) -> None:
        self.script = list(script)
        self.delay = delay
        self.attempts = 0
        self.attempt_times: list[float] = []
        self.attempted: list[str] = []
        self.in_flight = 0
        self.peak = 0

    def __call__(self, server):
        attempt = self.attempts
        self.attempts += 1
        self.attempt_times.append(time.monotonic())
        self.attempted.append(server.name)

        @asynccontextmanager
        async def _cm():
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await asyncio.sleep(self.delay)  # a real connect takes a moment
                item = self.script[attempt] if attempt < len(self.script) else self.script[-1]
                if isinstance(item, Exception):
                    raise item
                yield item
            finally:
                self.in_flight -= 1

        return _cm()


def _never_connects(server):
    @asynccontextmanager
    async def _cm():
        await asyncio.sleep(30)
        yield _session()

    return _cm()


def _raise(message: str):
    """Every attempt fails the same way."""

    def factory(server):
        @asynccontextmanager
        async def _cm():
            raise RuntimeError(message)
            yield  # pragma: no cover

        return _cm()

    return factory


@pytest.fixture
def fast_backoff(monkeypatch):
    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    monkeypatch.setattr(mcp_module, "_BACKOFF_MAX", 0.04)


# ---- R1: startup is concurrent, and it does not block the caller ----------


async def test_startup_is_concurrent_and_does_not_block(monkeypatch):
    """Eight dead servers cost two startup budgets, not eight — and ``open_all``
    returns before they settle at all (the operator's non-blocking choice)."""
    monkeypatch.setattr(mcp_module, "_MAX_CONCURRENT_CONNECTS", 4)
    factory = _Factory([RuntimeError("spawn failed")])
    manager = MCPManager(
        _cfg(*[_server(f"s{i}") for i in range(8)]), session_factory=factory
    )

    started = time.monotonic()
    await manager.open_all()
    assert time.monotonic() - started < 0.1  # scheduled, not settled

    assert await _until(lambda: all(s != "connecting" for s in _statuses(manager).values()))
    # Eight 200ms connections, four at a time: about two budgets, not eight
    # (1.6s serial).  The bound is corroboration — ``peak`` is the contract.
    assert time.monotonic() - started < 6 * 0.2
    assert factory.peak <= 4
    assert set(_statuses(manager).values()) == {"failed"}
    await manager.aclose()


# ---- R2/R3: a failed server keeps trying, on a growing pause ---------------


async def test_a_failing_server_retries_until_it_comes_back(fast_backoff):
    factory = _Factory([RuntimeError("cold"), RuntimeError("cold"), _session()])
    manager = MCPManager(_cfg(_server("late")), session_factory=factory)

    await manager.open_all()

    assert await _until(lambda: bool(manager.available()))
    assert factory.attempts == 3  # two failures, then it stayed
    assert _rows(manager)["late"]["status"] == "connected"
    await manager.aclose()


async def test_the_pause_grows_and_stays_under_the_cap(monkeypatch):
    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.02)
    monkeypatch.setattr(mcp_module, "_BACKOFF_MAX", 0.08)
    factory = _Factory([RuntimeError("nope")], delay=0.0)
    manager = MCPManager(_cfg(_server("dead")), session_factory=factory)

    await manager.open_all()
    await asyncio.sleep(0.6)
    gaps = [b - a for a, b in zip(factory.attempt_times, factory.attempt_times[1:], strict=False)]
    await manager.aclose()

    assert len(gaps) >= 3
    # The schedule's three facts: the first pause is the minimum, the next is
    # longer (the doubling), and none exceeds the cap.  At the cap each gap is
    # the cap plus scheduling noise, so "still growing" is not assertable and
    # is not the contract — the cap holding is.
    assert gaps[0] >= mcp_module._BACKOFF_MIN
    assert gaps[1] > gaps[0]
    assert all(gap <= mcp_module._BACKOFF_MAX + 0.02 for gap in gaps)
    assert len(gaps) >= 3  # it kept trying, which is the point of the loop


# ---- R4: a manual connect skips the pause ---------------------------------


async def test_a_manual_connect_tries_now_without_waiting_out_the_pause(monkeypatch):
    """MCP-08 renamed this: a wake cuts the wait short; it no longer resets the
    schedule.  "Try now" is not "forget the last failures"."""
    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 5.0)  # would wait forever
    monkeypatch.setattr(mcp_module, "_BACKOFF_MAX", 5.0)
    factory = _Factory([RuntimeError("cold"), _session()])
    manager = MCPManager(_cfg(_server("late")), session_factory=factory)

    await manager.open_all()
    assert await _until(lambda: factory.attempts == 1)

    started = time.monotonic()
    assert manager.request_connect("late") is True
    assert await _until(lambda: factory.attempts >= 2)
    assert time.monotonic() - started < 1.0  # did not wait out the 5s pause
    await manager.aclose()


def test_a_manual_connect_names_what_it_accepts():
    manager = MCPManager(
        _cfg(_server("fs"), _server("off", disabled=True)),
        session_factory=_ok_factory(),
    )
    assert manager.request_connect("fs") is True
    assert manager.request_connect("off") is False  # disabled
    assert manager.request_connect("nope") is False  # not configured


# ---- R5: a dead transport is noticed; an ordinary tool error is not -------


async def test_a_transport_failure_marks_the_server_and_wakes_a_retry(fast_backoff):
    import anyio

    attempts = {"n": 0}

    def factory(server):
        @asynccontextmanager
        async def _cm():
            # First attempt connects; after the transport dies the retry brings
            # it back, so the test can watch the server return on its own.
            attempts["n"] += 1
            healthy = attempts["n"] == 1
            yield _Scripted(raise_on_call=None if healthy else anyio.ClosedResourceError())

        return _cm()

    manager = MCPManager(_cfg(_server("fs")), session_factory=factory)
    await manager.open_all()
    tool = MCPCallTool(manager)
    assert await _until(lambda: bool(manager.available()))

    # Kill the transport under a healthy-looking session: the call is what
    # learns of the death (experiment: a parked owner never notices).
    manager._owner("fs").session._raise_on_call = anyio.ClosedResourceError()
    result = await tool.execute({"server": "fs", "tool": "read_file", "input": {}})
    assert result.success is False

    row = _rows(manager)["fs"]
    assert row["status"] == "failed"
    assert "重连" in row["detail"]
    assert manager.available() == []  # the catalog drops it at once
    await manager.aclose()


async def test_an_ordinary_tool_error_does_not_restart_the_server():
    factory_calls = {"n": 0}

    def factory(server):
        @asynccontextmanager
        async def _cm():
            factory_calls["n"] += 1
            yield _Scripted(raise_on_call=ValueError("bad arguments"))

        return _cm()

    manager = MCPManager(_cfg(_server("fs")), session_factory=factory)
    await manager.open_all()
    assert await _until(lambda: bool(manager.available()))

    tool = MCPCallTool(manager)
    for _ in range(3):
        await tool.execute({"server": "fs", "tool": "read_file", "input": {}})
    await asyncio.sleep(0.1)

    assert factory_calls["n"] == 1  # three failed calls, and still no restart
    assert _rows(manager)["fs"]["status"] == "connected"
    await manager.aclose()


# ---- R6/R7: one degraded per server, one event per settle -----------------


async def test_only_the_first_failure_is_reported_as_degraded(monkeypatch):
    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    monkeypatch.setattr(mcp_module, "_BACKOFF_MAX", 0.02)
    factory = _Factory([RuntimeError("spawn failed")])
    observer = RecordingObserver()
    manager = MCPManager(
        _cfg(_server("dead")), session_factory=factory, observer=observer
    )

    await manager.open_all()
    assert await _until(lambda: factory.attempts >= 4)
    await manager.aclose()

    degraded = observer.of_type(Degraded)
    assert len(degraded) == 1
    assert "dead" in degraded[0].reason


async def test_every_settle_publishes_a_status_event(monkeypatch):
    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    monkeypatch.setattr(mcp_module, "_BACKOFF_MAX", 0.02)
    events: list[tuple[str, dict]] = []
    factory = _Factory([RuntimeError("cold"), _session()])
    manager = MCPManager(_cfg(_server("late")), session_factory=factory)
    manager.attach_status_notifier(lambda method, params: events.append((method, params)))

    await manager.open_all()

    assert await _until(lambda: bool(manager.available()))
    assert [method for method, _params in events] == ["mcp.status.changed"] * 2
    assert all(params == {"server": "late"} for _method, params in events)
    await manager.aclose()


# ---- R8/R9: shutdown is clean, and a disabled server is left alone --------


async def test_aclose_stops_every_supervisor():
    manager = MCPManager(
        _cfg(*[_server(f"s{i}") for i in range(4)]), session_factory=_never_connects
    )
    await manager.open_all()
    tasks = list(manager._supervisors.values())
    await asyncio.sleep(0.05)

    await manager.aclose()

    assert all(task.done() for task in tasks)
    assert manager._supervisors == {}
    await manager.aclose()  # idempotent


async def test_a_disabled_server_is_never_attempted_and_says_why(fast_backoff):
    factory = _Factory([RuntimeError("nope")])
    manager = MCPManager(
        MCPConfig(
            enabled=True,
            servers=[_server("on"), _server("off", disabled=True)],
        ),
        session_factory=factory,
    )

    await manager.open_all()
    assert await _until(lambda: "on" in _statuses(manager))
    await asyncio.sleep(0.1)

    row = _rows(manager)["off"]
    assert row["status"] == "off"
    assert "禁用" in row["detail"]
    assert factory.attempted  # the manager did try
    assert set(factory.attempted) == {"on"}  # but never the disabled one
    await manager.aclose()


# ---- MCP-02: close waits for the owner task it just cancelled --------------


async def test_close_awaits_a_cancelled_owner_task(monkeypatch):
    """MCP-02: ``cancel()`` is a signal.  Returning on it leaves the session's
    ``AsyncExitStack`` (child process, stdio fd, HTTP client) unwound only on
    the loop's next tick — or never, when the process is exiting."""
    from ai_coding.core.mcp import _MCPSessionOwner

    exited: list[bool] = []

    @asynccontextmanager
    async def factory(server):
        try:
            yield _session()
        finally:
            # Cleanup that genuinely yields: this is the work the old close()
            # returned without waiting for.  A real exit stack finishes its
            # teardown even when the first cancellation lands inside it.
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
            exited.append(True)

    owner = _MCPSessionOwner(_server("fs"), factory)
    owner.start()
    assert await _until(lambda: owner.ready.is_set())

    monkeypatch.setattr(mcp_module, "_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr(mcp_module, "_CLOSE_CANCEL_GRACE", 1.0)
    await owner.close()

    assert owner._task.done() is True
    assert exited == [True]


async def test_close_is_bounded_even_if_a_session_ignores_cancellation(monkeypatch):
    """The 1s grace is a ceiling, not a promise: a stuck session must not make
    shutdown wait forever."""
    from ai_coding.core.mcp import _MCPSessionOwner

    escape = asyncio.Event()

    @asynccontextmanager
    async def factory(server):
        try:
            yield _session()
        finally:
            # Deliberately swallow cancellation, like a badly written context
            # manager that rewrites its own cancellation.
            while not escape.is_set():
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    continue
            exited.append(True)

    exited: list[bool] = []
    owner = _MCPSessionOwner(_server("fs"), factory)
    owner.start()
    assert await _until(lambda: owner.ready.is_set())

    monkeypatch.setattr(mcp_module, "_CLOSE_TIMEOUT", 0.05)
    monkeypatch.setattr(mcp_module, "_CLOSE_CANCEL_GRACE", 0.1)
    started = time.monotonic()
    await owner.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"close ignored its grace ({elapsed})"
    assert owner._task.done() is False  # bounded, not hung

    # Tidy up the task the production path correctly refused to wait for.
    escape.set()
    owner._task.cancel()
    await asyncio.wait({owner._task}, timeout=1.0)
    assert exited == [True]


async def test_close_is_idempotent_after_a_forced_cancel(monkeypatch):
    # Regression guard: idempotency held before the fix too.  It stays because
    # the added second ``asyncio.wait`` must not turn "close twice" into an
    # error on an already-done task.
    from ai_coding.core.mcp import _MCPSessionOwner

    @asynccontextmanager
    async def factory(server):
        try:
            yield _session()
        finally:
            await asyncio.sleep(0.05)

    owner = _MCPSessionOwner(_server("fs"), factory)
    owner.start()
    assert await _until(lambda: owner.ready.is_set())
    monkeypatch.setattr(mcp_module, "_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr(mcp_module, "_CLOSE_CANCEL_GRACE", 1.0)
    await owner.close()
    await owner.close()  # second close returns at once, no exception


# ---- MCP-06: remote servers can be reconnected, and the copy is right ------


async def test_a_remote_server_can_be_reconnected_manually():
    """MCP-06: a remote server has no command *by design*; the old
    ``not server.command`` guard made its "reconnect" button dead forever."""
    manager = MCPManager(
        _cfg(MCPServerConfig(name="r", type="remote", url="https://example.test/mcp"))
    )

    assert manager.request_connect("r") is True
    assert manager.request_connect("nope") is False


async def test_a_remote_server_without_a_url_says_url_not_command():
    manager = MCPManager(
        _cfg(MCPServerConfig(name="r", type="remote", url=""))
    )

    reason = manager.unreachable_reason("r")

    assert reason is not None
    assert "no url" in reason
    assert "command" not in reason


async def test_a_remote_connecting_row_advises_login_not_a_local_handshake():
    manager = MCPManager(MCPConfig(enabled=True, servers=[]))
    server = MCPServerConfig(
        name="r",
        type="remote",
        url="https://example.test/mcp",
        timeout=MCPTimeout(startup=60_000),
    )
    manager._attempt_started["r"] = time.monotonic() - 40

    detail = manager._connecting_detail(server)

    assert "登录" in detail
    assert "本地 server" not in detail


# ---- MCP-08: only a session that lasted may reset the backoff --------------


async def test_an_unstable_session_does_not_reset_the_backoff(monkeypatch):
    """A server that connects and drops immediately used to reset the pause to
    its minimum every cycle.  Three deaths in a row must grow, not repeat."""
    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.02)
    monkeypatch.setattr(mcp_module, "_BACKOFF_MAX", 0.4)
    monkeypatch.setattr(mcp_module, "_STABLE_SESSION_S", 3_600.0)
    factory = _Factory([_session()])
    manager = MCPManager(_cfg(_server("fs")), session_factory=factory)

    await manager.open_all()
    for _ in range(3):
        assert await _until(lambda: bool(manager.available()))
        manager.note_transport_death("fs")
    assert await _until(lambda: factory.attempts >= 4)
    gaps = [
        b - a
        for a, b in zip(factory.attempt_times, factory.attempt_times[1:], strict=False)
    ]
    await manager.aclose()

    assert len(gaps) >= 3
    assert gaps[1] > gaps[0], gaps
    assert gaps[2] > gaps[1], gaps


async def test_a_session_stable_for_a_minute_resets_the_backoff():
    from ai_coding.core.mcp import _MCPSessionOwner

    owner = _MCPSessionOwner(_server("fs"), _ok_factory())

    owner.ready_at = time.monotonic() - 120
    assert mcp_module._session_was_stable(owner) is True

    owner.ready_at = time.monotonic() - 5
    assert mcp_module._session_was_stable(owner) is False

    owner.ready_at = None
    assert mcp_module._session_was_stable(owner) is False


async def test_a_stable_session_gets_the_minimum_pause_again(monkeypatch):
    """The other half of MCP-08: the reset still exists, it just requires a
    session that actually lived."""
    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.02)
    monkeypatch.setattr(mcp_module, "_BACKOFF_MAX", 0.4)
    monkeypatch.setattr(mcp_module, "_STABLE_SESSION_S", 0.0)
    factory = _Factory([_session()])
    manager = MCPManager(_cfg(_server("fs")), session_factory=factory)

    await manager.open_all()
    for _ in range(3):
        assert await _until(lambda: bool(manager.available()))
        manager.note_transport_death("fs")
    assert await _until(lambda: factory.attempts >= 4)
    gaps = [
        b - a
        for a, b in zip(factory.attempt_times, factory.attempt_times[1:], strict=False)
    ]
    await manager.aclose()

    # Every cycle is a fresh minimum: no doubling.
    assert all(gap < 0.15 for gap in gaps), gaps

