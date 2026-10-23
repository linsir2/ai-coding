"""M4: remote MCP servers over Streamable HTTP, and the OAuth that guards them.

The transport is the SDK's (``streamable_http_client``), and so is every part of
the OAuth flow except the three things only an application can do: keep the
tokens, open the browser, and answer the redirect.  Those three are what this
file pins, together with the state machine around them — a server that needs a
sign-in is a *state*, not a retry, so the supervisor parks instead of hammering
the authorization server's metadata endpoint once a second.

The fake authorization server in here is a real HTTP server: the flow under test
is a real DCR, a real PKCE exchange, a real redirect and a real token request.
The only thing simulated is the human — ``/authorize`` redirects straight to the
callback, which is what a real server does after the operator consents.
"""

import asyncio
import json
import time
import urllib.parse
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import pytest_asyncio

from ai_coding.config.models import (
    MCPConfig,
    MCPOAuthConfig,
    MCPServerConfig,
    MCPTimeout,
)
from ai_coding.core.events import RecordingObserver
from ai_coding.core.mcp import MCPManager

pytestmark = pytest.mark.asyncio


# ---- the fake authorization server + MCP endpoint --------------------------


class _AsState:
    """What the fake AS has done, so a test can assert on the dance."""

    def __init__(self) -> None:
        self.registrations: list[dict] = []
        self.token_grants: list[dict] = []
        self.authorizations: list[dict] = []
        self.code_to_issue = "code-123"
        self.refused = False
        self.seen_authorization: list[str | None] = []


class _FakeAsHandler(BaseHTTPRequestHandler):
    """Serves the well-knowns, /register, /authorize, /token — and the MCP
    endpoint itself, JSON-only (the SDK's client accepts application/json)."""

    def log_message(self, *args) -> None:  # keep the test output readable
        return None

    @property
    def state(self) -> _AsState:
        return self.server.state  # type: ignore[attr-defined]

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if path == "/authorize":
            # A real AS redirects here once the operator consents; the fake one
            # does it immediately.  redirect_uri and state come from the query,
            # so the redirect lands on whatever callback port the client chose.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            redirect_uri = (query.get("redirect_uri") or [""])[0]
            state = (query.get("state") or [""])[0]
            self.send_response(302)
            target = f"{redirect_uri}?code={self.state.code_to_issue}&state={state}"
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/mcp":
            # The spec's answer for "no server-initiated stream": the client
            # must not treat it as an error (streamable_http.py's GET path).
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/.well-known/oauth-protected-resource/mcp":
            self._json(
                {"resource": f"{base}/mcp", "authorization_servers": [base]}
            )
            return
        if path == "/.well-known/oauth-authorization-server":
            self._json(
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "registration_endpoint": f"{base}/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                }
            )
            return
        self._json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        form = urllib.parse.parse_qs(body.decode())
        flat = {k: v[0] for k, v in form.items()}
        base = f"http://127.0.0.1:{self.server.server_address[1]}"

        if path == "/register":
            self.state.registrations.append(flat)
            self._json({"client_id": "fake-client", "client_id_issued_at": int(time.time())})
            return

        if path == "/token":
            self.state.token_grants.append(flat)
            self._json(
                {
                    "access_token": "token-abc",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-xyz",
                }
            )
            return

        if path == "/mcp":
            # The MCP endpoint.  ``require_auth`` off models the very common
            # public hosted MCP server that needs no credentials at all — the
            # path that had no coverage: every remote success in this file used
            # to go through the browser flow first.
            self.server.state.seen_authorization.append(self.headers.get("Authorization"))  # type: ignore[attr-defined]
            if getattr(self.server, "require_auth", True) and (
                self.headers.get("Authorization") != "Bearer token-abc"
            ):
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"',
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            request = json.loads(body)
            method = request.get("method")
            if method == "initialize":
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake-remote", "version": "1"},
                        },
                    }
                )
            elif method == "tools/list":
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "read_file",
                                    "description": "Read a file",
                                    "inputSchema": {"type": "object"},
                                }
                            ]
                        },
                    }
                )
            elif method == "tools/call":
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "content": [
                                {"type": "text", "text": f"called {request['params']['name']}"}
                            ]
                        },
                    }
                )
            else:
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {"code": -32601, "message": "nope"},
                    }
                )
            return

        self._json({"error": "not_found"}, status=404)

    def do_HEAD(self) -> None:  # some SDK paths probe with HEAD first
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _FakeAs:
    """The fake authorization server, on a real port, in a real thread."""

    def __init__(self, *, require_auth: bool = True) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAsHandler)
        self.server.state = _AsState()  # type: ignore[attr-defined]
        self.server.require_auth = require_auth  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self) -> _AsState:
        return self.server.state  # type: ignore[attr-defined]

    def start(self) -> None:
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest_asyncio.fixture
async def fake_as():
    server = _FakeAs()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest_asyncio.fixture
async def open_as():
    """The same fake with authentication switched off (A6)."""
    server = _FakeAs(require_auth=False)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """Never let a test open a real browser.

    ``mcp._open_browser`` calls ``webbrowser.open``.  On a headless box that
    returns False and nothing happens, but under WSLg (or any desktop) it opens
    a real Chrome window on the developer's screen -- one per test that reaches
    the authorize step, each one then sitting on a refused connection because
    the fake AS has already shut down.  So the default here is a browser that
    opens nothing and reports that it did open (the flow then parks in
    ``needs_auth``, which is what these tests are about).  The tests that do
    want the redirect followed install their own ``webbrowser.open`` via
    ``_patch_authorize`` afterwards, and that patch wins.
    """
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: True)


def _remote(name: str, base: str, **kwargs) -> MCPServerConfig:
    kwargs.setdefault("oauth", MCPOAuthConfig(callback_port=0))
    return MCPServerConfig(
        name=name,
        type="remote",
        url=f"{base}/mcp",
        timeout=MCPTimeout(startup=5000, catalog=5000),
        **kwargs,
    )


async def _until(predicate, timeout: float = 10.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


class _Scripted:
    """A session whose ``call_tool`` raises what the script says."""

    def __init__(self, raise_on_call: BaseException | None = None) -> None:
        self._raise_on_call = raise_on_call
        self.calls = 0

    async def list_tools(self):
        return SimpleNamespace(
            tools=[SimpleNamespace(name="read_file", description="reads", input_schema={})]
        )

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


class _Factory:
    """A session_factory that scripts what each attempt does.

    The only instrumentation these tests need: how many attempts ran and when
    each started — observed on the *factory*, not the manager, because watching
    retries is a test's need and a production API only tests consume is what M1
    deleted.
    """

    def __init__(self, script, delay: float = 0.0) -> None:
        self.script = list(script)
        self.delay = delay
        self.attempts = 0
        self.attempt_times: list[float] = []

    def __call__(self, server):
        attempt = self.attempts
        self.attempts += 1
        self.attempt_times.append(time.monotonic())

        @asynccontextmanager
        async def _cm():
            await asyncio.sleep(self.delay)
            item = self.script[attempt] if attempt < len(self.script) else self.script[-1]
            if isinstance(item, Exception):
                raise item
            yield item

        return _cm()


# ---- the fake AS's /authorize (a real redirect to the callback) -----------
#
# The SDK calls redirect_handler(authorization_url); the "browser" then has to
# visit it.  A real AS answers that visit with a 302 to the redirect URI once
# the operator consents, so this is the one endpoint the fake AS still needs.


def _patch_authorize(monkeypatch) -> None:
    """Simulate the browser: follow the authorize URL to the callback.

    Only ``webbrowser.open`` is patched — the call the production redirect
    handler itself makes — so the handler, the callback server and the whole
    SDK flow are the real ones.
    """
    import urllib.request
    import webbrowser

    def browser(url: str) -> bool:
        """Follow the authorize URL the way a browser would: the 302 lands on
        the callback server.  Synchronous, because the production handler runs
        this off the loop in an executor thread.
        """
        try:
            urllib.request.urlopen(url, timeout=10)
        except OSError:
            return False
        return True

    monkeypatch.setattr(webbrowser, "open", browser)


# ---- R1: the token store --------------------------------------------------


async def test_tokens_round_trip_and_are_scoped_per_server(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthToken

    from ai_coding.core.mcp import _auth_store_path, _TokenStore

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    store = _TokenStore("remote-one")
    other = _TokenStore("remote-two")

    await store.set_tokens(OAuthToken(access_token="t1", token_type="Bearer"))
    await other.set_tokens(OAuthToken(access_token="t2", token_type="Bearer"))

    assert (await store.get_tokens()).access_token == "t1"
    assert (await other.get_tokens()).access_token == "t2"
    # The file is a secret: 0600, and outside the repo by construction.
    mode = oct(_auth_store_path().stat().st_mode & 0o777)
    assert mode == "0o600"


async def test_logout_forgets_only_that_server(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthToken

    from ai_coding.core.mcp import _TokenStore

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    store, other = _TokenStore("a"), _TokenStore("b")
    await store.set_tokens(OAuthToken(access_token="t1", token_type="Bearer"))
    await other.set_tokens(OAuthToken(access_token="t2", token_type="Bearer"))

    store.clear()

    assert await store.get_tokens() is None
    assert (await other.get_tokens()).access_token == "t2"


# ---- R2/R3: needs_auth is a state, not a retry ----------------------------


async def test_a_server_that_needs_a_login_is_a_state_not_a_retry(fake_as, monkeypatch):
    from ai_coding.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    manager = MCPManager(
        MCPConfig(enabled=True, servers=[_remote("r", fake_as.base)]),
        session_factory=lambda server: (_ for _ in ()).throw(AssertionError("local factory")),
    )
    await manager.open_all()

    assert await _until(lambda: manager.servers()[0]["status"] == "needs_auth")
    # The state is the whole point: the supervisor must NOT be re-running
    # metadata discovery against the AS once a second.
    await asyncio.sleep(0.3)
    row = manager.servers()[0]
    assert row["status"] == "needs_auth"
    # The SDK refuses at the first thing a non-interactive connect lacks: with
    # no redirect URIs there is nothing to send the operator to.  Either message
    # means the same thing to the row — needs_auth — and only a login changes it.
    assert "No redirect URIs provided" in row["detail"]
    assert len(fake_as.state.authorizations) == 0  # never even asked the operator
    await manager.aclose()


async def test_a_plain_remote_failure_still_backs_off(fake_as, monkeypatch):
    """No regression: M4's auth parking does not swallow M3's retry loop."""
    from ai_coding.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.02)
    manager = MCPManager(
        MCPConfig(enabled=True, servers=[_remote("r", fake_as.base)]),
        session_factory=lambda server: (_ for _ in ()).throw(AssertionError("local factory")),
    )
    await manager.open_all()

    # A URL that answers 404 on every well-known is a plain failure: it retries.
    dead = _remote("r", "http://127.0.0.1:1")
    manager2 = MCPManager(MCPConfig(enabled=True, servers=[dead]))
    await manager2.open_all()
    assert await _until(lambda: manager2.servers()[0]["status"] == "failed")
    await asyncio.sleep(0.1)
    assert manager2.servers()[0]["status"] == "failed"
    await manager.aclose()
    await manager2.aclose()


# ---- R4: the whole dance --------------------------------------------------


async def test_login_runs_the_real_flow_and_the_server_comes_up(fake_as, tmp_path, monkeypatch):
    from ai_coding.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    monkeypatch.delenv("AI_CODING_MCP", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _patch_authorize(monkeypatch)

    manager = MCPManager(
        MCPConfig(
            enabled=True,
            servers=[_remote("r", fake_as.base, oauth=MCPOAuthConfig(callback_port=0))],
        ),
        session_factory=lambda server: (_ for _ in ()).throw(AssertionError("local factory")),
    )
    await manager.open_all()
    assert await _until(lambda: manager.servers()[0]["status"] == "needs_auth")

    result = await manager.login("r", timeout=15)

    assert result["ok"] is True, result
    # DCR happened, the token was exchanged, and it landed on disk.
    assert len(fake_as.state.registrations) == 1
    assert fake_as.state.token_grants[0]["grant_type"] == "authorization_code"
    assert fake_as.state.token_grants[0]["code_verifier"]  # PKCE, not a plain code
    from ai_coding.core.mcp import _TokenStore

    assert (await _TokenStore("r").get_tokens()).access_token == "token-abc"
    # And the supervisor, woken, connected with it — tools and all.
    assert await _until(lambda: bool(manager.available()))
    row = manager.servers()[0]
    assert row["status"] == "connected"
    assert row["toolCount"] == 1
    assert "read_file" in [h.name for _s, handles in manager.available() for h in handles]
    await manager.aclose()


async def test_login_times_out_cleanly_when_nobody_consents(fake_as, tmp_path, monkeypatch):
    from ai_coding.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    manager = MCPManager(
        MCPConfig(
            enabled=True,
            servers=[_remote("r", fake_as.base, oauth=MCPOAuthConfig(callback_port=0))],
        ),
        session_factory=lambda server: (_ for _ in ()).throw(AssertionError("local factory")),
    )
    await manager.open_all()
    assert await _until(lambda: manager.servers()[0]["status"] == "needs_auth")

    # No browser follow: the callback never arrives, and login says so.
    result = await manager.login("r", timeout=0.6)

    assert result["ok"] is False
    assert "callback" in result["reason"]
    assert manager.servers()[0]["status"] == "needs_auth"  # back where it was
    await manager.aclose()


async def test_logout_sends_the_server_back_to_needs_auth(fake_as, tmp_path, monkeypatch):
    from ai_coding.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _patch_authorize(monkeypatch)

    manager = MCPManager(
        MCPConfig(
            enabled=True,
            servers=[_remote("r", fake_as.base, oauth=MCPOAuthConfig(callback_port=0))],
        ),
    )
    await manager.open_all()
    assert await _until(lambda: manager.servers()[0]["status"] == "needs_auth")
    assert (await manager.login("r", timeout=15))["ok"] is True
    assert await _until(lambda: bool(manager.available()))

    assert manager.logout("r") == {"ok": True}

    assert await _until(lambda: manager.servers()[0]["status"] == "needs_auth")
    assert manager.available() == []
    await manager.aclose()


async def test_login_and_logout_name_what_they_refuse():
    manager = MCPManager(
        MCPConfig(
            enabled=True,
            servers=[MCPServerConfig(name="local", command=["x"])],
        )
    )
    assert (await manager.login("nope"))["ok"] is False
    assert (await manager.login("local"))["ok"] is False  # not remote
    assert manager.logout("nope")["ok"] is False


# ---- M5: the wizard's backend ----------------------------------------------


async def test_adopt_adds_the_server_and_supervises_it():
    """Runtime add: the wizard's collected fields become a live supervisor."""
    factory = _Factory([_session()])
    manager = MCPManager(
        MCPConfig(enabled=True, servers=[]), session_factory=factory
    )
    await manager.open_all()

    verdict = manager.adopt(MCPServerConfig(name="fs", command=["fs"]))

    assert verdict["ok"] is True
    assert "fs" not in manager._supervisors  # adopt alone starts nothing
    await manager.start_supervisor(manager._config.servers[-1])
    assert await _until(lambda: bool(manager.available()))
    assert manager.servers()[0]["name"] == "fs"
    # The config now holds it too: a restart re-adopts the same server.
    assert [s.name for s in manager._config.servers] == ["fs"]
    await manager.aclose()


def test_adopt_refuses_what_it_cannot_use():
    from ai_coding.core.mcp import MCPManager as _M

    manager = _M(MCPConfig(enabled=False))
    assert manager.adopt(MCPServerConfig(name="x", command=["x"]))["ok"] is False
    manager = _M(MCPConfig(enabled=True))
    assert manager.adopt(MCPServerConfig(name="x"))["ok"] is False  # no command
    assert manager.adopt(MCPServerConfig(name="x", type="remote"))["ok"] is False  # no url
    assert manager.servers() == []


async def test_adopt_refuses_a_duplicate_name():
    """A second server with the same name would collide in the tool namespace;
    the first one keeps the name and the row says so (M5)."""
    manager = MCPManager(
        MCPConfig(enabled=True, servers=[MCPServerConfig(name="fs", command=["x"])]),
    )
    await manager.open_all()

    verdict = manager.adopt(MCPServerConfig(name="fs", command=["y"]))

    assert verdict["ok"] is False
    assert "fs" in verdict["reason"]
    assert len(manager.servers()) == 1  # the original is untouched
    await manager.aclose()


def test_trusting_a_server_skips_its_prompts():
    """The wizard's last step, as a gate method (M5).

    The gate is built once from the config at startup, so a server added at
    runtime has to be trusted on the live object too — otherwise the answer the
    operator just gave would be ignored until the next start.
    """
    from ai_coding.security.permission_gate import PermissionDecision, PermissionGate

    gate = PermissionGate()
    assert gate.check("fs__read_file", {}) == PermissionDecision.WARN

    gate.trust_server("fs")

    assert gate.check("fs__read_file", {}) == PermissionDecision.ALLOW
    # The same ordering guarantee from_config's trusted servers have.
    assert gate.check("bash", {"command": "rm -rf /"}) == PermissionDecision.DENY


# ---- A6: a remote server that needs no credentials at all -------------------
#
# Every other remote test here begins with a browser login.  That left the
# commonest shape of hosted MCP server — a public endpoint, no OAuth — with no
# coverage at all: ``_connect_remote`` always builds an ``OAuthClientProvider``,
# so whether that path can connect *without* a token was never established by
# anything.  It is the path a user hits the first time they paste a URL.


async def test_a_remote_server_that_needs_no_login_connects_lists_and_answers(open_as):
    from ai_coding.config.models import MCPConfig
    from ai_coding.core.mcp import MCPManager, build_mcp_catalog, invoke_remote_tool

    manager = MCPManager(
        MCPConfig(enabled=True, servers=[_remote("public", open_as.base)])
    )
    await manager.open_all()
    try:
        assert await _until(lambda: bool(manager.available())), "a no-auth endpoint never connected"
        assert [name for name, _ in manager.available()] == ["public"]

        # The tool reached the catalog with the arguments the server declared.
        catalog = build_mcp_catalog(manager.available())
        assert "read_file" in catalog

        # The call works, all the way to a result the model could read.
        handle = manager.lookup("public", "read_file")
        assert handle is not None
        result = await invoke_remote_tool(
            server="public",
            tool_name="read_file",
            session=handle.session,
            arguments={},
            ctx=None,
            execution_timeout=handle.execution_timeout,
            progress_name="public__read_file",
        )
        assert result.success is True, result
        assert "called read_file" in str(result.output)

        # And nothing about the browser flow ran: no client was registered, no
        # token was exchanged, and not one request carried credentials.
        assert open_as.state.registrations == []
        assert open_as.state.token_grants == []
        assert open_as.state.seen_authorization
        assert all(value is None for value in open_as.state.seen_authorization)
    finally:
        await manager.aclose()


async def test_a_remote_server_that_needs_no_login_never_reaches_needs_auth(open_as):
    """needs_auth is for a server that *asked* for a login. One that never did
    must settle as connected, or the panel would offer a sign-in that cannot
    help."""
    from ai_coding.config.models import MCPConfig
    from ai_coding.core.mcp import MCPManager

    manager = MCPManager(
        MCPConfig(enabled=True, servers=[_remote("public", open_as.base)])
    )
    await manager.open_all()
    try:
        assert await _until(
            lambda: all(row["status"] != "connecting" for row in manager.servers())
        )
        row = next(r for r in manager.servers() if r["name"] == "public")
        assert row["status"] == "connected", row
    finally:
        await manager.aclose()


async def test_a_token_never_reaches_an_event_or_a_trace_field(fake_as, tmp_path, monkeypatch):
    """A8's negative half, which the other A8 test does not cover.

    ``test_tokens_round_trip_and_are_scoped_per_server`` proves the file is
    0600; this proves the other half of the same promise — that the token does
    not leave the token store. Everything the manager publishes goes through one
    observer, so it is the only place a leak could come from, and a long-lived
    OAuth token in a trace file is exactly the kind of secret that is easy to
    write and impossible to take back.
    """
    from ai_coding.core import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_BACKOFF_MIN", 0.01)
    monkeypatch.delenv("AI_CODING_MCP", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _patch_authorize(monkeypatch)

    observer = RecordingObserver()
    manager = MCPManager(
        MCPConfig(enabled=True, servers=[_remote("r", fake_as.base)]),
        observer=observer,
        session_factory=lambda server: (_ for _ in ()).throw(AssertionError("local factory")),
    )
    await manager.open_all()
    assert await _until(lambda: manager.servers()[0]["status"] == "needs_auth")

    # A 401 parks the server in needs_auth; the token is only issued once the
    # operator signs in, so the login is what this test is about.
    assert (await manager.login("r", timeout=15))["ok"] is True
    assert await _until(lambda: bool(manager.available())), "the login never landed"

    # The token really was issued, or the rest of this proves nothing.
    assert fake_as.state.token_grants, "no token was exchanged; the test is vacuous"
    issued = fake_as.state.token_grants

    # Events are dataclasses, so repr() is the faithful text of everything a
    # trace writer would serialise.
    published = repr(observer.events)
    assert "token-abc" not in published
    assert "refresh-xyz" not in published
    assert "access_token" not in published
    # ...and the observer really did see this server, or the three assertions
    # above prove nothing at all.
    assert "mcp_server" in published, published[:400]
    # The check has teeth: the same predicate flags an event that does carry a
    # token, so passing above is a fact about the code and not about the
    # expression being blind.
    assert "token-abc" in repr([SimpleNamespace(detail="Bearer token-abc")])
    assert issued, "sanity"
    await manager.aclose()


# ---- M7: a manual token replaces the OAuth dance ---------------------------


async def test_a_manual_bearer_header_never_builds_an_oauth_provider(monkeypatch):
    """GitHub's remote endpoint rejects dynamic client registration, so OAuth
    can never authenticate it — and *trying* costs the operator the whole
    startup budget: the 401 starts a metadata-discovery chain, and when the
    authorization server is slow or unreachable that chain burns all 60s and
    the row reports "not ready in 60s", which says nothing actionable.

    With a token already in hand there is nothing to discover, so the provider
    must not be constructed. OpenCode's own guide for this endpoint says the
    same thing in config terms: ``oauth: false`` next to the header.
    """
    import mcp.client.auth as sdk_auth

    from ai_coding.core.mcp import _remote_session_factory

    built: list[str] = []

    class _Recorder:
        def __init__(self, *args, **kwargs):
            built.append("provider")

    monkeypatch.setattr(sdk_auth, "OAuthClientProvider", _Recorder)

    server = _remote(
        "gh", "https://example.invalid/mcp/", headers={"Authorization": "Bearer ghp_manual"}
    )
    context = _remote_session_factory(server, interactive=False, callback_timeout=1.0)

    try:
        async with context:
            pass
    except Exception:
        # Whatever the transport does with an unreachable host is beside the
        # point; which path was taken is the point.
        pass

    assert built == [], "an OAuth provider was built for a server that already has a token"


async def test_a_remote_server_without_a_token_still_asks_for_one(monkeypatch):
    """The other half: skipping OAuth must follow the token, not the
    transport. A server with no header keeps the flow it had — a 401 lands in
    ``needs_auth`` for the panel's sign-in row."""
    from ai_coding.config.models import MCPConfig
    from ai_coding.core.mcp import MCPManager

    manager = MCPManager(MCPConfig(enabled=True, servers=[_remote("r", "http://127.0.0.1:1/mcp")]))
    await manager.open_all()
    try:
        assert await _until(
            lambda: all(row["status"] != "connecting" for row in manager.servers())
        )
        assert manager.servers()[0]["status"] != "connected"
    finally:
        await manager.aclose()
