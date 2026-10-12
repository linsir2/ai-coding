"""MCP integration over the ``mcp`` package directly (R1a-6).

Replaces the M5 implementation, which produced ``agents.mcp.MCPServerStdio``
objects for the OpenAI Agents SDK.  The manager connects the configured servers
and exposes their tools through the same permission gate and approval path as
local ones.

M2 changed *how*: one ``mcp_call`` dispatcher (``tools/mcp_call.py``) carries
every remote call instead of one registered ``BaseTool`` per remote tool,
because a schema per tool is re-sent on every step — 13 tools measured 13,301
characters per step (docs/features.md 538-541).  What the model reads is the
bounded catalog (``build_mcp_catalog``), regenerated per step from live state by
the caller, so it rides the loop's volatile tail rather than a frozen schema.
``MCPToolAdapter`` stays as the per-tool execution kernel both paths share.

Design notes (docs/r1a-6-analysis.md §3.2):
  - the transport is injectable, so tests need no subprocess and no live server;
  - tool names are prefixed with the server name, since two servers may expose
    the same tool name;
  - a server that fails to connect is skipped silently: MCP is an enhancement.

R2d: a connection is bounded by ``timeout.startup`` (a server that never answers
must not cost the CLI its startup), and ``environment``/``cwd`` reach the child
process — a real stdio server usually needs at least one of them.

M1: three budgets, as OpenCode v2 splits them (docs/specs/2026-10-10-external-mcp.md
§3.1). ``startup`` bounds connect+handshake, ``catalog`` bounds ``tools/list``
(milliseconds both), ``execution`` bounds one remote call and defaults to off,
leaving the turn deadline the only bound.  ``command`` is argv as one list.

Bounding a connection is subtler than it looks: the MCP client is anyio-based and
anyio cancel scopes belong to the task that entered them, so connecting inside a
deadline task and closing from the caller fails with "Attempted to exit cancel
scope in a different task than it was entered in" (observed in a real run, see
docs/r2-analysis.md R2d).  Hence :class:`_MCPSessionOwner`: **one task owns a
server's session from connect to close.**
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import logging
import os
import tempfile
import urllib.parse as urllib_parse
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.config.models import MCPConfig, MCPOAuthConfig, MCPServerConfig
from ai_coding.core.events import ToolChunk
from ai_coding.core.turn_report import report_degraded
from ai_coding.domain.message import ToolResult
from ai_coding.infra.deadline import DeadlineExpired, run_with_deadline
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

SessionFactory = Callable[[MCPServerConfig], Any]

_log = logging.getLogger(__name__)

_DEV_NULL: Any = None


def _devnull() -> Any:
    """The process-wide ``/dev/null``, opened once.

    Being a *real* file is the whole requirement: ``stdio_client`` hands it to
    ``anyio``, which asks for its ``fileno``.
    """
    global _DEV_NULL
    if _DEV_NULL is None:
        _DEV_NULL = open(os.devnull, "w")  # noqa: SIM115 — lives for the process
    return _DEV_NULL


#: The stderr capture of the attempt being connected, if the owner set one.
#: Only alive across ``enter_async_context`` — that is when ``anyio`` asks the
#: file for its ``fileno`` and dups it into the child.
_current_errlog: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "mcp_errlog", default=None
)

# How long a *connected* session may take to shut down before it is cancelled.
# A session that never connected is cancelled almost immediately instead.
_CLOSE_TIMEOUT = 5.0
_CLOSE_TIMEOUT_NEVER_CONNECTED = 0.2

#: Description length per tool in the catalog, as OpenCode bounds it
#: (packages/core/src/codemode/catalog.ts:48).
_CATALOG_DESCRIPTION_LIMIT = 120

#: With a signature on the line, the description has less to say: the argument
#: names and types were the half of it that was load-bearing.  Measured on the
#: 13-tool server, 120→80 together with signatures is *cheaper* than 120 alone
#: (1804 vs 1865 characters), so this is not a concession.
_CATALOG_DESCRIPTION_LIMIT_SIGNED = 80

#: A rendered signature's own width, and the nesting depth it will follow.  The
#: width is what keeps one wide tool from eating the budget; the depth is what
#: keeps a hostile or self-referential schema from taking the turn down with
#: it — both are total-function guards, not budget policy.
_SIGNATURE_MAX_CHARS = 160
_SIGNATURE_MAX_DEPTH = 8

#: ~4 characters per token, so this is OpenCode's INLINE_BUDGET of 2,000 tokens
#: (catalog.ts:50).  Over it, configured servers are dropped, loudly.
_CATALOG_BUDGET_CHARS = 8_000

#: Characters held back for the trailing "omitted" note, sized for three digits
#: so it fits whatever the count turns out to be.
#:
#: Held for the whole build rather than checked at the end, because the note is
#: only *sometimes* needed and "sometimes" is what let a 240-character budget
#: produce 282 characters.  The cost is that an unused reserve is thrown away:
#: 45 characters, 0.6% of the budget, in exchange for a bound that cannot be
#: violated by any input.
_OMITTED_NOTE_RESERVE = len("(999 more server(s) omitted: catalog budget)") + 1

#: The pause between a server's connect attempts, doubling up to the cap (M3).
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 60.0

#: How many connects may run at once.  Spawning is the expensive half of a
#: connect (npx cold starts measured ~27s, docs/features.md), so ten servers
#: configured means ten cold starts at once — the bound OpenCode's reconcile
#: lacks and its own review names.  The bound is also why the backoff needs no
#: jitter: a room full of servers retrying in lockstep is absorbed here.
_MAX_CONCURRENT_CONNECTS = 4

#: How long ``mcp.login`` waits for the browser to come back.  A human reading
#: a consent screen is slow, but not indefinitely slow.
_LOGIN_TIMEOUT = 300.0


def _is_transport_death(exc: BaseException) -> bool:
    """Whether an exception means the *connection* is gone, not just this call.

    A parked owner never reads the stream, so a subprocess that exits mid
    session is invisible until a call fails (M3 §1) — and only a dead transport
    justifies tearing the session down.  A bad argument is not that.
    anyio is imported here, lazily, so this module still loads without the MCP
    SDK; by the time a call fails the SDK is loaded anyway.
    """
    from anyio import BrokenResourceError, ClosedResourceError, EndOfStream

    return isinstance(
        exc, (ClosedResourceError, BrokenResourceError, EndOfStream, ConnectionError)
    )


@dataclass(frozen=True)
class MCPToolHandle:
    """One callable remote tool: what to call, on which session, under what cap.

    The single source both the catalog text and the dispatcher read, so what the
    model is told and what it can actually invoke cannot drift apart.
    """

    server: str
    name: str
    #: The remote tool as the server described it (schema, description).
    tool: Any
    session: Any
    execution_timeout: float | None
    description: str = ""


def _schema_signature(
    schema: Any,
    defs: Mapping[str, Any],
    depth: int = 0,
) -> str:
    """One remote tool's call signature: ``(title: string, body?: string)``.

    The MCP spec *requires* ``inputSchema`` on ``tools/list``, and every handle
    already carries the server's own schema (``MCPToolHandle.tool``).  Without
    this the model was told a tool existed and nothing about how to call it —
    the only clue to the argument names was whatever prose the description
    happened to contain.  OpenCode compiles the same schema into a TypeScript
    signature for its Code Mode catalog
    (``packages/codemode/src/tool-schema.ts``); this is that compiler, with
    three guards it needs and this catalog does not:

    * a **depth** cap, so a self-referential schema ends in ``unknown`` instead
      of recursing until the turn is dead;
    * a **width** cap, so one 40-argument tool cannot spend the budget alone —
      the omitted arguments are *said*, because a silently truncated signature
      teaches the model a grammar the server does not have;
    * local ``$ref`` only.  A ``$ref`` to an http URL is not resolved: fetching
      a URL while rendering a catalog would turn a text format into an SSRF
      surface, and no real tool schema needs it.

    Total by construction: every branch returns a string, and no input can make
    it raise — a malformed schema yields ``unknown``, not a failed turn.
    """
    if depth >= _SIGNATURE_MAX_DEPTH:
        return "unknown"
    if not isinstance(schema, Mapping):
        return "unknown"

    ref = schema.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/"):
            return "unknown"
        path = ref[2:].split("/")
        # ``defs`` is already the ``$defs`` (or ``definitions``) map, and the
        # pointer starts by naming it, so the first segment is skipped.
        if path and path[0] in ("$defs", "definitions"):
            path = path[1:]
        target: Any = defs
        for part in path:
            if not isinstance(target, Mapping):
                return "unknown"
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part)
        if target is None:
            return "unknown"
        return _schema_signature(target, defs, depth + 1)

    kind = schema.get("type")
    if kind == "array":
        return f"Array<{_schema_signature(schema.get('items'), defs, depth + 1)}>"
    if kind == "object" or (kind is None and "properties" in schema):
        return _object_signature(schema, defs, depth)
    if isinstance(kind, list):
        # JSON Schema 2020-12 allows a type union; the first member that names
        # itself is as good an answer as the catalog can give.
        for member in kind:
            rendered = _schema_signature({**schema, "type": member}, defs, depth)
            if rendered != "unknown":
                return rendered
        return "unknown"
    if kind in ("string", "integer", "number", "boolean", "null"):
        return str(kind)
    return "unknown"


def _object_signature(schema: Mapping[str, Any], defs: Mapping[str, Any], depth: int) -> str:
    parts = _object_parts(schema, defs, depth)
    return "object" if parts is None else "{" + ", ".join(parts) + "}"


def _object_parts(
    schema: Mapping[str, Any], defs: Mapping[str, Any], depth: int
) -> list[str] | None:
    """``None`` when there is nothing to enumerate, else the rendered members.

    The members come back without their delimiters because the root prints them
    as an argument list ``(a, b)`` while a nested object prints as ``{a, b}``.
    """
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return None
    required = {str(name) for name in (schema.get("required") or [])}
    parts: list[str] = []
    omitted = False
    for raw_name, raw_value in properties.items():
        name = str(raw_name)
        rendered = f"{_safe_identifier(name)}: {_schema_signature(raw_value, defs, depth + 1)}"
        if name not in required:
            rendered = rendered.replace(":", "?:", 1)
        candidate = f"{', '.join(parts)}, {rendered}" if parts else rendered
        # Width is checked as the line is built so the cut lands between
        # arguments, never inside one.
        if len(candidate) > _SIGNATURE_MAX_CHARS - 4:
            omitted = True
            break
        parts.append(rendered)
    if omitted:
        parts.append("…")
    return parts


def _safe_identifier(name: str) -> str:
    """A JSON-schema key is not necessarily an identifier (``content-type``)."""
    plain = all(c.isalnum() or c == "_" for c in name)
    if name and plain and (name[0].isalpha() or name[0] == "_"):
        return name
    return f'"{name}"'


def tool_signature(handle: MCPToolHandle) -> str:
    """The signature to print after a tool's name, or ``""`` when it has none.

    Empty means "the server sent nothing to render": ``build_mcp_catalog`` then
    prints exactly the line it printed before this existed, because a broken
    server should not change the shape of every other line.
    """
    tool = getattr(handle, "tool", None)
    schema = getattr(tool, "input_schema", None)
    if not isinstance(schema, Mapping):
        return ""
    defs = schema.get("$defs") or schema.get("definitions") or {}
    if not isinstance(defs, Mapping):
        defs = {}
    properties = schema.get("properties")
    parts = _object_parts(schema, defs, 0)
    if isinstance(properties, Mapping) and not properties:
        return "()"
    return "(" + ", ".join(parts or []) + ")"


def _tool_line(handle: MCPToolHandle) -> str:
    """One catalog line for one tool: name, signature, description."""
    signature = tool_signature(handle)
    description = (handle.description or "").strip().replace("\n", " ")
    limit = _CATALOG_DESCRIPTION_LIMIT_SIGNED if signature else _CATALOG_DESCRIPTION_LIMIT
    if len(description) > limit:
        description = description[:limit] + "…"
    label = f"  {handle.name}{signature}"
    return f"{label}: {description}" if description else label


def build_mcp_catalog(
    servers: list[tuple[str, list[MCPToolHandle]]],
    budget_chars: int = _CATALOG_BUDGET_CHARS,
) -> str:
    """The catalog text: one entry per callable remote tool (M2).

    Pure, and deliberately so — the budget discipline is the whole reason this
    exists, and it is testable without a connection.

    Order is the config's (see :meth:`MCPManager.available`), and the budget is
    **shared round-robin**: every server gets one tool before any server gets a
    second.  It used to fill servers in order and drop whole trailing ones,
    which produced the worst outcome this function can produce — a connected,
    working server the model cannot see, behind a panel row that says
    "Connected ✓".  A server that gets no room at all is now *named* with its
    tool count rather than dropped silently, and a server whose header itself
    does not fit is counted in the trailing line.

    The header is counted against the budget too.  It used to be added after
    the accounting, so a full 8,000 budget actually produced ~8,200 characters
    and the documented bound was not the enforced one.
    """
    if not servers:
        return ""
    header = (
        "<mcp-catalog>\n"
        "Remote tools, called with mcp_call(server, tool, input).\n"
        "server and tool must be spelled exactly as listed here.\n"
    )
    lines: list[str] = []
    # Every character of the output is accounted for before anything is added:
    # the opening header, the newline after it, and the closing tag. Leaving the
    # tag out is how a 240-character budget produced 282 characters.
    used = len(header) + 1 + len("</mcp-catalog>") + _OMITTED_NOTE_RESERVE
    shown = [0] * len(servers)
    taken = [0] * len(servers)

    progressed = True
    while progressed:
        progressed = False
        for index, (_name, handles) in enumerate(servers):
            if taken[index] >= len(handles):
                continue
            line = _tool_line(handles[taken[index]])
            # A server's header is paid for by the first tool it gets.
            cost = len(line) + 1 + (len(_name) + 1 if shown[index] == 0 else 0)
            if used + cost > budget_chars:
                continue
            if shown[index] == 0:
                lines.append(_name)
                shown[index] = 1
            lines.append(line)
            used += cost
            taken[index] += 1
            progressed = True

    omitted = 0
    for index, (name, handles) in enumerate(servers):
        if not handles or shown[index] or taken[index] == len(handles):
            continue
        line = f"{name} ({len(handles)} tools, none shown)"
        # The note's room was already held back above; this line may only spend
        # what is left of it.
        if used + len(line) + 1 > budget_chars:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if not lines:
        # Nothing at all fitted.  A catalog whose whole content is "(3 more
        # server(s) omitted)" tells the model nothing it could act on, and the
        # header alone is larger than a small budget — so an empty catalog is
        # the honest answer, and it is the one ``build_mcp_catalog([])`` already
        # means.  This also keeps ``len(text) <= budget_chars`` true for every
        # budget, instead of only for budgets above the header's own size.
        return ""
    if omitted:
        lines.append(f"({omitted} more server(s) omitted: catalog budget)")
    return header + "\n".join(lines) + "\n</mcp-catalog>"


class _MCPSessionOwner:
    """Owns one server's session, from connect to close, inside a single task."""

    def __init__(self, server: MCPServerConfig, factory: SessionFactory) -> None:
        self.server = server
        self.connected = asyncio.Event()
        self.ready = asyncio.Event()
        self.tools: list[Any] = []
        self.session: Any = None
        self.error: BaseException | None = None
        #: Set when a call learned the transport is gone (M3 §1).  Keeps the
        #: owner installed so rows can say why, while ``available()`` stops
        #: offering it.
        self.dead: str | None = None
        #: Which budget ran out, if one did: "startup" / "catalog" (M1).
        self.failed_phase: str | None = None
        #: The child's stderr for this attempt (a real file: ``anyio`` dups its
        #: fd into the child).  Most spawn failures are visible only here.
        self.stderr_file = tempfile.TemporaryFile()
        self._factory = factory
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def stderr_tail(self, limit: int = 300) -> str:
        """The last of what the child wrote to stderr, for the row's detail.

        Empty when nothing was captured or nothing was written.
        """
        try:
            self.stderr_file.seek(0)
            return self.stderr_file.read().decode("utf-8", "replace").strip()[-limit:]
        except (OSError, ValueError):  # closed file, or a stream that cannot seek
            return ""

    def start(self) -> None:
        self._task = asyncio.create_task(self._serve(), name=f"mcp:{self.server.name}")

    def mark_dead(self, reason: str) -> None:
        """Record that this session's transport is gone (a call learned of it).

        The owner stays installed — the row has to be able to say *why* the
        server is gone — but stops being a source of tools, so the catalog
        drops them on the same step rather than after the reconnect.
        """
        if self.dead is None:
            self.dead = reason

    async def _serve(self) -> None:
        """Connect, publish the tool list, hold the session, then close it here."""
        try:
            async with AsyncExitStack() as stack:
                token = _current_errlog.set(self.stderr_file)
                try:
                    self.session = await stack.enter_async_context(self._factory(self.server))
                finally:
                    _current_errlog.reset(token)
                self.connected.set()
                listed = await self.session.list_tools()
                self.tools = list(getattr(listed, "tools", None) or [])
                self.ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — reported through .error
            self.error = exc
            # A failure is itself a kind of "settled": without this the startup
            # budget would be spent in full on a server that already failed.
            self.connected.set()
            self.ready.set()

    async def wait_ready(
        self, startup_s: float, catalog_s: float
    ) -> tuple[bool, str | None]:
        """Wait for the connection, then for the tool list, under two budgets.

        ``(ok, phase)``: ``phase`` is ``None`` when the server is usable,
        otherwise which budget ran out ("startup" / "catalog").  A server that
        failed rather than timed out reports ``(False, "error")`` — the caller
        reads the reason from ``.error``.
        """
        for phase, budget_s in (("startup", startup_s), ("catalog", catalog_s)):
            try:
                await run_with_deadline(
                    (self.connected if phase == "startup" else self.ready).wait(),
                    timeout=budget_s,
                    label=f"mcp_{phase}:{self.server.name}",
                )
            except DeadlineExpired:
                self.failed_phase = phase
                _log.warning(
                    "MCP server %r did not finish %s within %gs",
                    self.server.name,
                    phase,
                    budget_s,
                )
                return False, phase
            if self.error is not None:
                _log.debug("MCP server %r failed: %r", self.server.name, self.error)
                return False, "error"
        return True, None

    async def close(self) -> None:
        """Release the session in *its own* task (cancel it if it will not go)."""
        self._stop.set()
        task = self._task
        if task is None or task.done():
            return
        budget = _CLOSE_TIMEOUT if self.ready.is_set() else _CLOSE_TIMEOUT_NEVER_CONNECTED
        done, _pending = await asyncio.wait({task}, timeout=budget)
        if not done:
            task.cancel()


def _auth_store_path() -> Path:
    """Where the OAuth tokens live: outside the repo and outside the config.

    ``~/.local/share/ai-coding/mcp-auth.json`` — the config file is meant to be
    shareable and this file is meant to be a secret, so they never meet.
    """
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "ai-coding" / "mcp-auth.json"


class _TokenStore:
    """``TokenStorage`` over one JSON file, scoped to one server (M4).

    The SDK's protocol is per-connection (get/set tokens and client info); the
    file is per-process so every server's tokens share it, keyed by name.  A
    token is a secret: the file is created 0600, and nothing here ever puts it
    in the config, a trace event or a log line.
    """

    def __init__(self, server: str) -> None:
        self._server = server
        self._path = _auth_store_path()

    def _read(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write(self, doc: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 from the start: a token file that is briefly
        # world-readable is a token file that leaked.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False, indent=2)

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("servers", {}).get(self._server, {}).get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: Any) -> None:
        doc = self._read()
        doc.setdefault("servers", {}).setdefault(self._server, {})["tokens"] = (
            tokens.model_dump(mode="json")
        )
        self._write(doc)

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("servers", {}).get(self._server, {}).get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: Any) -> None:
        doc = self._read()
        doc.setdefault("servers", {}).setdefault(self._server, {})["client_info"] = (
            client_info.model_dump(mode="json")
        )
        self._write(doc)

    def clear(self) -> None:
        """Forget this server's tokens (``mcp.logout``)."""
        doc = self._read()
        servers = doc.get("servers", {})
        if self._server in servers:
            del servers[self._server]
            self._write(doc)


class _CallbackServer:
    """The local endpoint an authorization redirect returns to (M4).

    One request and then it closes: the OAuth callback is a single GET carrying
    the code, and a listener that stayed up would be a port held open for a call
    that is never coming again.  The SDK validates the ``state`` against the one
    it generated, so this only has to hand back what arrived.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._server: asyncio.Server | None = None
        self._code: asyncio.Future[tuple[str, str]] | None = None

    async def start(self) -> None:
        self._code = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        # The configured port may be taken (another login in progress, another
        # program); the OS assigns one and the redirect URI is built from it, so
        # the operator is never sent to a port nothing is listening on.
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readline()
            target = request.decode("latin-1").split(" ")[1] if request else "/"
            query = urllib_parse.parse_qs(urllib_parse.urlparse(target).query)
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            error = (query.get("error") or [""])[0]
            if error:
                body = f"Sign-in failed: {error}".encode()
            else:
                body = b"Signed in - you can close this window."
                if self._code is not None and not self._code.done():
                    self._code.set_result((code, state))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except Exception:  # noqa: BLE001 — a half-written callback is not fatal
            pass
        finally:
            writer.close()

    async def result(self, timeout: float) -> Any:
        """Wait for the browser to come back with a code."""
        from mcp.client.auth import AuthorizationCodeResult

        if self._code is None:
            raise RuntimeError("callback server was never started")
        try:
            code, state = await asyncio.wait_for(self._code, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise OAuthLoginTimeout(f"no sign-in callback within {timeout:g}s") from exc
        return AuthorizationCodeResult(code=code, state=state or None)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


class OAuthLoginTimeout(Exception):
    """The operator never came back from the browser (M4)."""


def _find_leaf(exc: BaseException | None, cls: type) -> BaseException | None:
    """The first leaf of ``exc`` that is a ``cls``, walking anyio's groups.

    A transport failure arrives wrapped — the group's own ``str()`` says only
    "unhandled errors in a TaskGroup" — so everything that classifies or quotes
    an error goes through this instead of reading the top level.
    """
    if exc is None:
        return None
    if isinstance(exc, cls):
        return exc
    for sub in getattr(exc, "exceptions", None) or []:
        found = _find_leaf(sub, cls)
        if found is not None:
            return found
    return None


def _is_auth_failure(exc: BaseException | None) -> bool:
    """Whether a connect failed because a human has to sign in (M4).

    Two shapes, one meaning: the SDK's ``OAuthFlowError`` for everything the
    authorization flow could not complete, and our own ``OAuthLoginTimeout``
    for a sign-in nobody finished.  Both say "this server is waiting on a
    login", and neither is changed by retrying on a timer.
    """
    from mcp.client.auth import OAuthFlowError

    return (
        _find_leaf(exc, OAuthFlowError) is not None
        or _find_leaf(exc, OAuthLoginTimeout) is not None
    )


def _startup_detail(server: MCPServerConfig) -> str:
    """Why a connect ran out of time, in the words that let the operator act.

    A local server's startup is the process not coming up, and the sentence
    already says so. A remote one is a different failure wearing the same
    clothes: the 401 starts a metadata-discovery chain, and if the network or
    the authorization server stalls, the chain burns the whole budget and the
    operator is told only that it was not ready. Measured against GitHub's
    endpoint on one machine: 4.1s, 15.1s, then 60.2s and a dead end. Both
    causes are things the operator can check.
    """
    seconds = server.timeout.startup / 1000
    if server.type == "remote":
        return (
            f"未在 {seconds:g}s 内就绪"
            "（远程：查网络是否可达，以及该端的授权服务器是否响应；"
            "若它要 token，请在向导里填 API token）"
        )
    return f"未在 {seconds:g}s 内就绪"


def _manual_bearer(server: MCPServerConfig) -> bool:
    """Whether the config already carries its own credential (M7).

    One question, asked once, at the only place it matters: a header the
    operator supplied *is* the answer to the challenge an OAuth provider would
    otherwise turn into a browser flow.
    """
    return any(str(name).lower() == "authorization" for name in server.headers)


def _remote_session_factory(
    server: MCPServerConfig, *, interactive: bool, callback_timeout: float
) -> Any:
    """Connect a remote MCP server over Streamable HTTP (M4).

    ``interactive`` is the whole difference between a connect and a login: with
    it, a 401 drives the browser and the callback; without it, the same 401 ends
    the flow immediately and the server lands in ``needs_auth`` — a state, not a
    retry.
    """
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    oauth = server.oauth or MCPOAuthConfig()
    headers = dict(server.headers)

    @asynccontextmanager
    async def _cm() -> AsyncIterator[Any]:
        callback: _CallbackServer | None = None
        if interactive:
            callback = _CallbackServer(oauth.callback_port)
            await callback.start()
        try:
            manual = _manual_bearer(server)
            if interactive and manual and callback is not None:
                # The operator already gave a token; opening a browser would ask
                # them for one they have.
                await callback.stop()
                callback = None
            redirect_uri = (
                f"http://127.0.0.1:{callback.port}/mcp/oauth/callback"
                if callback is not None
                else None
            )
            store = _TokenStore(server.name)
            # With a manual bearer there is nothing to discover: the provider's
            # whole job is to turn a 401 into a login, and a header already
            # answers the challenge.  Building it anyway is not merely wasted —
            # against an endpoint that refuses dynamic client registration
            # (GitHub's, by its own documentation) the discovery chain runs,
            # fails, retries, and consumes the whole startup budget before the
            # operator sees anything worse than "not ready in 60s".  Measured:
            # 4.1s / 15.1s / 60.2s for the same request on the same machine.
            provider = None if manual else OAuthClientProvider(
                server_url=server.url,
                client_metadata=OAuthClientMetadata(
                    client_name="ai-coding",
                    redirect_uris=[AnyUrl(redirect_uri)] if redirect_uri else None,
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=oauth.scope or None,
                ),
                storage=store,
                redirect_handler=_open_browser if interactive else None,
                callback_handler=(
                    (lambda: callback.result(callback_timeout))
                    if callback is not None
                    else None
                ),
            )
            import httpx2

            async with streamable_http_client(
                server.url,
                http_client=httpx2.AsyncClient(auth=provider, headers=headers or None),
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    # ``ClientSession.__aenter__`` only starts the dispatcher:
                    # the handshake is a separate (idempotent) call, and for a
                    # remote server it is also the request that discovers the
                    # token is missing and starts the flow.
                    await session.initialize()
                    yield session
        finally:
            if callback is not None:
                await callback.stop()

    return _cm()


async def _open_browser(url: str) -> None:
    """Send the operator to the authorization URL (async: the SDK awaits it).

    ``webbrowser.open`` blocks — it may fork a browser and wait for it — so it
    runs off the loop.  It returns False on a headless machine, and the URL is
    logged either way, because an operator over SSH has to be able to paste it
    somewhere.
    """
    import webbrowser

    opened = await asyncio.get_running_loop().run_in_executor(
        None, webbrowser.open, url
    )
    if not opened:
        _log.warning("open this URL to sign in: %s", url)


def _server_parameters(server: MCPServerConfig) -> Any:
    """Build ``StdioServerParameters`` (kept separate so it can be unit-tested)."""
    from mcp import StdioServerParameters

    command = list(server.command)
    return StdioServerParameters(
        command=command[0] if command else "",
        args=command[1:],
        env=dict(server.environment) or None,
        cwd=server.cwd or None,
    )


def _default_session_factory(server: MCPServerConfig) -> Any:
    """Connect a stdio MCP server and yield an initialized ``ClientSession``."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    @asynccontextmanager
    async def _cm() -> AsyncIterator[Any]:
        # The child's stderr is captured, not printed: a server that fails its
        # spawn (a missing script, a package that never installed) says so only
        # there, and with a supervisor retrying every few seconds the operator's
        # terminal would fill with another copy of the same complaint.  The
        # capture must be a *real* file — ``anyio`` asks it for a ``fileno``
        # and dups that into the child, which then writes it directly.
        async with stdio_client(
            _server_parameters(server),
            errlog=_current_errlog.get() or _devnull(),
        ) as (read, write):
            async with ClientSession(read, write) as session:
                # ``ClientSession.__aenter__`` only starts the dispatcher: the
                # handshake is a separate (idempotent) call.  Without it a real
                # server rejects every request with -32602 "Invalid request
                # parameters" — the R1a-6 factory never sent it, and only the
                # fake-session unit tests hid that (found by the R2d real run).
                await session.initialize()
                yield session

    return _cm()


def _join_text(content: Any) -> str:
    """Concatenate the text blocks of a ``CallToolResult.content`` list."""
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


async def invoke_remote_tool(
    *,
    server: str,
    tool_name: str,
    session: Any,
    arguments: dict[str, Any],
    ctx: ToolContext | None,
    execution_timeout: float | None,
    progress_name: str,
    on_transport_error: Callable[[], None] | None = None,
) -> ToolResult:
    """Call one remote tool and normalize whatever comes back (M2 kernel).

    The single execution path for MCP: the dispatcher calls it directly, and
    :class:`MCPToolAdapter` is a thin adapter over it for the gated real-connection
    tests.  Everything it can go wrong stays inside: a remote error, an is_error
    result or an expired call becomes a ``ToolResult``, because an MCP exception
    must never reach the turn (docs/data-contracts.md:126).
    """
    live = resolve_ctx(ctx)
    steps = itertools.count()

    def _progress(progress: float, total: float | None = None, message: str | None = None) -> None:
        body = message or (f"{progress}/{total}" if total else str(progress))
        live.emit(
            ToolChunk(
                call_id=live.call_id,
                tool_name=progress_name,
                stream="progress",
                text=str(body),
                seq=next(steps),
            )
        )

    try:
        if execution_timeout is not None:
            result = await run_with_deadline(
                session.call_tool(tool_name, arguments, progress_callback=_progress),
                timeout=execution_timeout,
                label=f"mcp_call:{tool_name}",
            )
        else:
            result = await session.call_tool(
                tool_name, arguments, progress_callback=_progress
            )
    except DeadlineExpired:
        return ToolResult(
            success=False,
            output=f"error: MCP tool '{tool_name}' did not answer within "
            f"{execution_timeout:g}s",
            error="mcp_error",
            data={"server": server, "tool": tool_name},
        )
    except Exception as exc:  # noqa: BLE001 — tool boundary
        if on_transport_error is not None and _is_transport_death(exc):
            # The session is gone, not just this call: whoever owns the server
            # needs to know before the model tries again.
            on_transport_error()
        return ToolResult(
            success=False,
            output=f"error: MCP tool '{tool_name}' failed: {exc}",
            error="mcp_error",
            data={"server": server, "tool": tool_name},
        )

    is_error = bool(getattr(result, "is_error", False))
    output = _join_text(getattr(result, "content", None))
    if not output:
        output = (
            "error: MCP tool reported a failure"
            if is_error
            else "(empty MCP result)"
        )
    return ToolResult(
        success=not is_error,
        output=output,
        data={
            "server": server,
            "tool": tool_name,
            "is_error": is_error,
            "structured": getattr(result, "structured_content", None),
        },
        error="mcp_error" if is_error else None,
    )


class MCPToolAdapter(BaseTool):
    """The per-tool shape of a remote call, kept for the real-connection tests.

    M2 routes calls through ``mcp_call`` instead of registering one of these per
    tool, so production no longer builds them; ``tests/test_mcp_stdio.py`` drives
    this class against a real subprocess, which is why it stays.
    """

    # Remote side effects are unknown, so never run these concurrently.
    parallel_safe = False

    def __init__(
        self,
        *,
        server: str,
        tool: Any,
        session: Any,
        execution_timeout: float | None = None,
    ) -> None:
        self._server = server
        self._tool = tool
        self._session = session
        self._execution_timeout = execution_timeout

    @property
    def name(self) -> str:
        return f"{self._server}__{getattr(self._tool, 'name', 'tool')}"

    @property
    def origin(self) -> str:
        """The server this tool belongs to (R7e-3: the tool surface is per server)."""
        return self._server

    @property
    def description(self) -> str:
        return str(getattr(self._tool, "description", "") or "")

    @property
    def params_json_schema(self) -> dict[str, Any]:
        raw = getattr(self._tool, "input_schema", None)
        schema: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        return await invoke_remote_tool(
            server=self._server,
            tool_name=str(getattr(self._tool, "name", "")),
            session=self._session,
            arguments=dict(params),
            ctx=ctx,
            execution_timeout=self._execution_timeout,
            progress_name=self.name,
        )


class MCPManager:
    """Connects the configured MCP servers and keeps them connected (M3).

    Startup is non-blocking: :meth:`open_all` starts one supervisor per server
    and returns, so the TUI appears immediately and a row says ``connecting``
    until its first attempt settles.  Each supervisor then retries its server
    forever — a doubling pause capped at ``_BACKOFF_MAX`` — until the process
    ends, the server is disabled, or it comes back.

    The one hard lesson in here: a server can only be known dead *by trying to
    use it*.  After connecting, the owner parks in ``_stop.wait()`` and never
    reads the stream, so a subprocess that exits mid-session is invisible
    (measured: the task sits unbothered while the child is gone).  So a
    transport-class call failure marks the server, drops it from
    :meth:`available`, and wakes its supervisor; an ordinary tool error does
    none of that, because restarting a server over a bad argument is theatre.
    """

    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        session_factory: SessionFactory | None = None,
        observer: Any | None = None,
    ) -> None:
        self._config = config or MCPConfig()
        self._session_factory = session_factory or _default_session_factory
        self._observer = observer
        # Settled owners, in config order (``available`` reads them in that
        # order, which is the catalog's order: first configured, first listed).
        self._owners: list[_MCPSessionOwner] = []
        self._supervisors: dict[str, asyncio.Task[None]] = {}
        #: Per-server "try now" flags: a manual connect, or a dead transport.
        self._wake: dict[str, asyncio.Event] = {}
        #: Servers already reported degraded once (M3: the first failure only).
        self._degraded: set[str] = set()
        self._stop = asyncio.Event()
        self._notifier: Callable[[str, Any], None] | None = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CONNECTS)

    # -- lifecycle ----------------------------------------------------------

    async def open_all(self) -> None:
        """Start a supervisor per connectable server; do not wait for any of them.

        Idempotent, so a composition root built twice cannot double the retry
        loops, and inert after :meth:`aclose`.
        """
        if not self._config.enabled or self._supervisors or self._stop.is_set():
            return
        seen: set[str] = set()
        for server in self._config.servers:
            if server.disabled or not server.name:
                continue
            if not _is_connectable_shape(server):
                continue
            if server.name in seen:
                # Two servers sharing a name produce identical tool prefixes, which
                # the registry rejects; keep the first and move on rather than
                # letting a config typo escape as a CLI traceback.
                continue
            seen.add(server.name)
            await self.start_supervisor(server)

    def adopt(self, server: MCPServerConfig) -> dict[str, Any]:
        """Take a server into the running process (the wizard's backend, M5).

        Adds it to the config this manager holds — so ``servers()`` and a later
        restart's ``load`` see the same set — and starts the same supervisor
        ``open_all`` would have.  Returns the verdict rather than raising: the
        caller is a panel, and "a server by that name already exists" is an
        answer it should show, not a traceback.
        """
        if not self._config.enabled:
            return {"ok": False, "reason": "MCP is disabled (mcp.enabled is false)"}
        if any(existing.name == server.name for existing in self._config.servers):
            return {
                "ok": False,
                "reason": f"a server named '{server.name}' already exists",
            }
        if not _is_connectable_shape(server):
            missing = "command" if server.type == "local" else "url"
            return {"ok": False, "reason": f"a {server.type} server needs a {missing}"}
        self._config.servers.append(server)
        self._publish(server.name)
        return {"ok": True}

    async def start_supervisor(self, server: MCPServerConfig) -> bool:
        """Give one server its retry loop, if it does not have one (M5).

        Separated from :meth:`adopt` because they run in different threads:
        adopt is called from the IPC serve thread (a plain data change), while
        this creates a task and must therefore run on the manager's loop — the
        IPC layer marshals it with ``service.spawn``, the same wheel
        ``session.turn`` uses.  Calling it directly is what ``open_all`` does.
        """
        if self._stop.is_set() or server.name in self._supervisors:
            return False
        self._supervisors[server.name] = asyncio.create_task(
            self._supervise(server), name=f"mcp-sup:{server.name}"
        )
        return True

    async def _supervise(self, server: MCPServerConfig) -> None:
        """One server's connect-retry-hold loop, for the life of the process."""
        delay = _BACKOFF_MIN
        previous: _MCPSessionOwner | None = None
        try:
            while not self._stop.is_set():
                if previous is not None:
                    await previous.close()
                    previous = None
                owner = _MCPSessionOwner(
                    server, self._factory_for(server, interactive=False)
                )
                ok, phase = await self._attempt(owner)
                if self._stop.is_set():
                    await owner.close()
                    return
                previous = owner
                if ok:
                    self._install(owner)
                    self._publish(server.name)
                    await self._hold(server.name)
                    if self._stop.is_set():
                        return
                    # A wake while healthy means either a manual reconnect or a
                    # dead transport: either way this session is finished.
                    self._mark_dead(server.name, "连接已断开，正在重连")
                    self._publish(server.name)
                    delay = _BACKOFF_MIN
                else:
                    self._install(owner)
                    self._publish(server.name)
                    if server.name not in self._degraded:
                        self._degraded.add(server.name)
                        report_degraded(
                            self._observer,
                            "mcp_server",
                            _fail_reason(server, owner, phase),
                            "its tools are not available this run",
                        )
                    if _is_auth_failure(owner.error):
                        # Waiting on a human, not on time: retrying on a timer
                        # would re-run metadata discovery against the
                        # authorization server every second for an answer only a
                        # login can change.  Park until one arrives.
                        await self._hold(server.name)
                        if self._stop.is_set():
                            return
                        delay = _BACKOFF_MIN
                        continue
                    woken = await self._pause(server.name, delay)
                    if woken:
                        delay = _BACKOFF_MIN
                    else:
                        delay = min(delay * 2, _BACKOFF_MAX)
        finally:
            if previous is not None:
                try:
                    await previous.close()
                except Exception:  # noqa: BLE001 — shutting down must not raise
                    _log.debug(
                        "MCP session %r failed to close",
                        previous.server.name,
                        exc_info=True,
                    )

    async def _attempt(self, owner: _MCPSessionOwner) -> tuple[bool, str | None]:
        """Connect and list, bounded by the spawn bound and the server's budgets."""
        async with self._semaphore:
            owner.start()
            return await owner.wait_ready(
                owner.server.timeout.startup / 1000,
                owner.server.timeout.catalog / 1000,
            )

    async def _hold(self, name: str) -> None:
        """Park while the server is up; returns on shutdown or a wake.

        Clears the wake it consumed, the way ``_pause`` does: a flag left set
        after the wait makes the *next* park return immediately, and the loop
        then reconnects the healthy server over and over.
        """
        stop = asyncio.create_task(self._stop.wait())
        wake = asyncio.create_task(self._wake_event(name).wait())
        try:
            await asyncio.wait({stop, wake}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
            wake.cancel()
            self._wake_event(name).clear()

    async def _pause(self, name: str, delay: float) -> bool:
        """Wait out the backoff, or cut it short on a wake.  True means woken."""
        timer = asyncio.create_task(asyncio.sleep(delay))
        wake = asyncio.create_task(self._wake_event(name).wait())
        try:
            done, _pending = await asyncio.wait(
                {timer, wake}, return_when=asyncio.FIRST_COMPLETED
            )
            return wake in done
        finally:
            timer.cancel()
            wake.cancel()
            self._wake_event(name).clear()

    def _factory_for(
        self,
        server: MCPServerConfig,
        *,
        interactive: bool,
        callback_timeout: float = _LOGIN_TIMEOUT,
    ) -> SessionFactory:
        """The transport for this server's kind (M4).

        The injected factory stays the seam for ``local`` (and for the tests
        that refuse to spawn anything); ``remote`` is built here because its
        pieces — the SDK transport, the token store, the callback server — are
        exactly what this module owns.
        """
        if server.type == "remote":
            return lambda _s: _remote_session_factory(
                server, interactive=interactive, callback_timeout=callback_timeout
            )
        return self._session_factory

    async def login(self, name: str, timeout: float = _LOGIN_TIMEOUT) -> dict[str, Any]:
        """Run the OAuth dance for a remote server (``mcp.login``).

        This obtains and persists the token and nothing else: the session it
        used to get there is closed again, and the supervisor — woken at the
        end — is what owns the server's connection from then on.  One extra
        handshake buys a design where exactly one component holds a session.
        """
        server = next((s for s in self._config.servers if s.name == name), None)
        if server is None:
            return {"ok": False, "reason": f"no MCP server named '{name}'"}
        if server.type != "remote":
            return {"ok": False, "reason": f"server '{name}' is not a remote server"}
        owner = _MCPSessionOwner(
            server,
            self._factory_for(
                server, interactive=True, callback_timeout=timeout
            ),
        )
        owner.start()
        try:
            # The callback's own timeout is the one the operator should read,
            # so the overall wait is a backstop behind it, not a race with it.
            ok, _phase = await owner.wait_ready(timeout + 5.0, 30.0)
        finally:
            await owner.close()
        if not ok:
            timeout_error = _find_leaf(owner.error, OAuthLoginTimeout)
            reason = (
                str(timeout_error)
                if timeout_error is not None
                else f"sign-in failed: {_error_detail(owner.error)}"
            )
            # The settled owner still carries the *connect-time* refusal, which
            # is now stale: the operator tried to sign in and this is why it did
            # not take.  Recording it keeps the row the single source of truth.
            settled = self._owner(name)
            if settled is not None:
                settled.error = owner.error or OAuthLoginTimeout(reason)
            self._publish(name)
            return {"ok": False, "reason": reason}
        # The token is on disk now (the provider persisted it), so a plain
        # connect will find it: wake the supervisor and let it do its job.
        self._wake_event(name).set()
        self._publish(name)
        return {"ok": True}

    def logout(self, name: str) -> dict[str, Any]:
        """Forget a remote server's tokens (``mcp.logout``).

        The supervisor is woken, so the server is re-attempted at once and lands
        back in ``needs_auth`` instead of pretending to be connected.
        """
        server = next((s for s in self._config.servers if s.name == name), None)
        if server is None:
            return {"ok": False, "reason": f"no MCP server named '{name}'"}
        _TokenStore(name).clear()
        self._wake_event(name).set()
        return {"ok": True}

    def _wake_event(self, name: str) -> asyncio.Event:
        return self._wake.setdefault(name, asyncio.Event())

    def _install(self, owner: _MCPSessionOwner) -> None:
        """Make ``owner`` the settled record for its server, in place.

        Superseded sessions are closed by the supervisor that replaced them
        (its ``previous``), so this only swaps references.
        """
        for index, existing in enumerate(self._owners):
            if existing.server.name == owner.server.name:
                self._owners[index] = owner
                return
        self._owners.append(owner)

    def _owner(self, name: str) -> _MCPSessionOwner | None:
        for owner in self._owners:
            if owner.server.name == name:
                return owner
        return None

    def _mark_dead(self, name: str, reason: str) -> None:
        owner = self._owner(name)
        if owner is not None:
            owner.mark_dead(reason)

    def _publish(self, name: str) -> None:
        """Announce that a server's state settled (``mcp.status.changed``).

        The notifier is attached by ``make_handlers`` once the IPC service
        exists; anything published before then can have no listener, and the
        panel's own fetch-on-open covers the initial state, so dropping is
        safe rather than lossy.
        """
        if self._notifier is None:
            return
        try:
            self._notifier("mcp.status.changed", {"server": name})
        except Exception:  # noqa: BLE001 — observation must not break the loop
            _log.debug("mcp status publish failed for %r", name, exc_info=True)

    def attach_status_notifier(self, notifier: Callable[[str, Any], None]) -> None:
        """Where status changes go once the IPC service exists (``make_handlers``)."""
        self._notifier = notifier

    def request_connect(self, name: str) -> bool:
        """Wake a server's supervisor for an immediate attempt (``mcp.connect``).

        False for anything not connectable right now — unknown, disabled, or
        the master switch off.  The panel only offers this on a failed row, so
        this is the guard behind that button.
        """
        if not self._config.enabled:
            return False
        server = next((s for s in self._config.servers if s.name == name), None)
        if server is None or server.disabled or not server.command:
            return False
        self._wake_event(name).set()
        return True

    def note_transport_death(self, name: str) -> None:
        """Learn that a call to ``name`` failed on a dead transport (M3 §1).

        The only death signal a parked owner can get.  Marks the server so rows
        and the catalog drop it at once, and wakes the supervisor — the return
        is the point of the exercise.  Idempotent: a batch of calls failing
        together is one death.
        """
        owner = self._owner(name)
        if owner is None or owner.dead is not None:
            return
        owner.mark_dead("连接已断开，正在重连")
        self._wake_event(name).set()
        self._publish(name)

    # -- what the rest of the system reads -----------------------------------

    def available(self) -> list[tuple[str, list[MCPToolHandle]]]:
        """Connected servers and the handles the dispatcher may call (M2).

        Whitelist applies (an R7d filter is a budget decision, not a
        permission one), permissions deliberately do not: a tool the gate
        refuses must still be *listed* in the catalog, or its refusal reads as
        a bug.  A dead transport excludes its server immediately — the catalog
        drops it on the same step, rather than offering calls that cannot run.

        **Order is the config's, not the connect race's.**  ``_owners`` is
        appended as each server finishes connecting, so iterating it sorted
        servers by startup time; the catalog's budget then dropped whichever
        the operator had listed *last*.  Config order is what the budget's own
        docstring promises, so it is read from the config instead.  A server
        the wizard added at runtime is appended to that config and therefore
        sorts last, which is also the only stable answer for it.
        """
        rows: list[tuple[str, list[MCPToolHandle]]] = []
        order = {server.name: index for index, server in enumerate(self._config.servers)}
        for owner in sorted(self._owners, key=lambda o: order.get(o.server.name, len(order))):
            if owner.error is not None or owner.dead is not None or not owner.ready.is_set():
                continue
            server = owner.server
            wanted = {str(name) for name in server.tools or []}
            handles: list[MCPToolHandle] = []
            for tool in owner.tools:
                remote = str(getattr(tool, "name", ""))
                if wanted and remote not in wanted:
                    continue
                handles.append(
                    MCPToolHandle(
                        server=server.name,
                        name=remote,
                        tool=tool,
                        session=owner.session,
                        execution_timeout=(
                            server.timeout.execution / 1000
                            if server.timeout.execution is not None
                            else None
                        ),
                        description=str(getattr(tool, "description", "") or ""),
                    )
                )
            if handles:
                rows.append((server.name, handles))
        return rows

    def lookup(self, server: str, tool: str) -> MCPToolHandle | None:
        """One callable handle, or ``None`` when the pair is not callable."""
        for name, handles in self.available():
            if name == server:
                for handle in handles:
                    if handle.name == tool:
                        return handle
        return None

    def unreachable_reason(self, server: str) -> str | None:
        """Why a *configured* server cannot be called, for the model's benefit."""
        cfg = next((s for s in self._config.servers if s.name == server), None)
        if cfg is None:
            return None
        owner = self._owner(server)
        if owner is not None and _is_auth_failure(owner.error):
            return (
                f"MCP server '{server}' needs authentication: "
                f"{_error_detail(owner.error)}. Sign in from the MCP panel."
            )
        if owner is not None and owner.dead is not None:
            return (
                f"MCP server '{server}' is not connected: its connection dropped "
                "and a retry is in progress."
            )
        if owner is not None and owner.error is not None:
            detail = str(owner.error) or type(owner.error).__name__
            return (
                f"MCP server '{server}' is not connected: {detail}. "
                "Reconnect it from the MCP panel."
            )
        if owner is not None and owner.failed_phase is not None:
            return (
                f"MCP server '{server}' is not connected: it did not settle in "
                "time. Reconnect it from the MCP panel."
            )
        if owner is None:
            if cfg.disabled:
                return f"MCP server '{server}' is disabled."
            if not cfg.command:
                return f"MCP server '{server}' has no command configured."
            if server in self._supervisors:
                return f"MCP server '{server}' is still connecting."
            return (
                f"MCP server '{server}' is not connected. "
                "Reconnect it from the MCP panel."
            )
        return None

    @property
    def enabled(self) -> bool:
        """Whether the master switch is on (``mcp.enabled``).

        Read by the panel and the wizard so they can say so before the
        operator answers three questions, instead of ``adopt`` refusing at
        the end of them.
        """
        return self._config.enabled

    def servers(self) -> list[dict[str, Any]]:
        """Every configured server and what became of it (MCP-MCP §2.1).

        Two sources are needed and neither is enough alone: ``_config.servers``
        is what the operator asked for, ``_owners`` is what happened.  A server
        skipped before an owner existed (no name, no command, a duplicate name)
        only exists in the config, and a server whose command is wrong only
        exists in ``_owners`` — reporting one side would make the other
        disappear from the dialog, which is worse than reporting a failure.

        A server whose first attempt has not settled yet says ``connecting``
        rather than borrowing a failure it does not have yet (M3: startup is
        non-blocking, so that state is observable by design).
        """
        owners = {owner.server.name: owner for owner in self._owners}
        rows: list[dict[str, Any]] = []
        for server in self._config.servers:
            wanted = sorted({str(name) for name in getattr(server, "tools", None) or []})
            owner = owners.get(server.name)
            row: dict[str, Any] = {
                "name": server.name,
                "command": " ".join(server.command).strip(),
                "status": "failed",
                "toolCount": 0,
                "wantedCount": len(wanted),
                "detail": "",
            }
            if not self._config.enabled:
                row["status"] = "off"
            elif server.disabled:
                # M1 flipped the polarity; the row used to fall through to the
                # duplicate-name reason here, which named the wrong defect.
                row["status"] = "off"
                row["detail"] = "该 server 已禁用"
            elif owner is None:
                # No owner means open_all never even tried: name, command, a
                # duplicate name — or no attempt has settled yet.
                if not server.name:
                    row["detail"] = "配置缺少 name"
                elif not server.command and server.type == "local":
                    row["detail"] = "配置缺少 command"
                elif not server.url and server.type == "remote":
                    row["detail"] = "配置缺少 url"
                elif server.name in self._supervisors:
                    row["status"] = "connecting"
                    row["detail"] = "正在连接…"
                else:
                    row["detail"] = "与同名服务器重复，只连了第一个"
            elif owner.dead is not None:
                row["detail"] = owner.dead
            elif _is_auth_failure(owner.error):
                row["status"] = "needs_auth"
                row["detail"] = _error_detail(owner.error)
            elif owner.error is not None:
                # An exception can carry no message (``raise RuntimeError``), so
                # fall back to its class name rather than showing an empty row.
                # anyio wraps a dead child in nested groups whose str() is always
                # "unhandled errors in a TaskGroup" — unactionable, so the leaves
                # are what the row says.
                row["detail"] = _error_detail(owner.error)
                tail = owner.stderr_tail()
                if tail and tail not in row["detail"]:
                    row["detail"] = f"{row['detail']}: {tail}"
            elif owner.failed_phase == "catalog":
                row["detail"] = f"工具列表未在 {server.timeout.catalog / 1000:g}s 内返回"
            elif owner.failed_phase == "startup" or not owner.ready.is_set():
                row["detail"] = _startup_detail(server)
            elif not owner.tools:
                row["detail"] = "已连接，但没有工具"
            else:
                listed = sorted({str(getattr(t, "name", "")) for t in owner.tools})
                # Filtered = the server offered it and the whitelist did not
                # want it — the other direction is a whitelist naming a tool the
                # server does not have, which is a different (quieter) problem.
                skipped = [name for name in listed if name not in wanted]
                if wanted and skipped:
                    row["status"] = "filtered"
                    row["toolCount"] = len(listed) - len(skipped)
                    row["detail"] = f"已过滤：{'、'.join(skipped[:8])}"
                    if len(skipped) > 8:
                        row["detail"] += f" 等 {len(skipped)} 个"
                else:
                    row["status"] = "connected"
                    row["toolCount"] = len(listed)
            rows.append(row)
        return rows

    async def aclose(self) -> None:
        """Stop every supervisor and tear down every session (idempotent)."""
        self._stop.set()
        supervisors, self._supervisors = self._supervisors, {}
        for task in supervisors.values():
            task.cancel()
        if supervisors:
            await asyncio.gather(*supervisors.values(), return_exceptions=True)
        owners, self._owners = self._owners, []
        for owner in owners:
            try:
                await owner.close()
            except Exception:  # noqa: BLE001 — shutting down must not raise
                _log.debug("MCP session %r failed to close", owner.server.name, exc_info=True)


def _error_detail(exc: BaseException | None, depth: int = 0) -> str:
    """What an operator can act on, out of whatever the SDK raised.

    ``anyio`` reports a child that died as nested ExceptionGroups, and the
    group's own ``str()`` is always "unhandled errors in a TaskGroup (1
    sub-exception)" — it names no file, no errno and no cause.  The leaves are
    the real story, so the row shows them.
    """
    if exc is None:
        return ""
    # Duck-typed rather than ``isinstance(..., BaseExceptionGroup)``: anyio's
    # groups are builtins, but the project does not pin a target version, and
    # "has .exceptions" is exactly the question being asked.
    leaves = getattr(exc, "exceptions", None)
    if leaves and depth <= 4:
        parts = [_error_detail(sub, depth + 1).strip() for sub in leaves]
        parts = [part for part in parts if part]
        if parts:
            return "; ".join(dict.fromkeys(parts))
    return str(exc) or type(exc).__name__


def _is_connectable_shape(server: MCPServerConfig) -> bool:
    """Whether this server has what its kind needs to be attempted at all (M4).

    ``local`` needs a command and ``remote`` needs a URL; the two shapes share
    nothing else, so one missing field is the whole check — and the row says
    which one, so a half-configured server never vanishes from the panel.
    """
    if server.type == "remote":
        return bool(server.url)
    return bool(server.command)


def _fail_reason(server: MCPServerConfig, owner: _MCPSessionOwner, phase: str | None) -> str:
    """The degraded line for one failed attempt — which budget ran out, or why."""
    if phase == "error":
        return f"server '{server.name}': {owner.error!r}"
    if phase == "catalog":
        return (
            f"server '{server.name}': tool listing did not finish "
            f"within {server.timeout.catalog / 1000:g}s"
        )
    return (
        f"server '{server.name}': did not become ready within "
        f"{server.timeout.startup / 1000:g}s"
    )


__all__ = ["MCPManager", "MCPToolAdapter", "build_mcp_catalog"]
