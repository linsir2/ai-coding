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
import itertools
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from ai_coding.config.models import MCPConfig, MCPServerConfig
from ai_coding.core.events import ToolChunk
from ai_coding.core.turn_report import report_degraded
from ai_coding.domain.message import ToolResult
from ai_coding.infra.deadline import DeadlineExpired, run_with_deadline
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

SessionFactory = Callable[[MCPServerConfig], Any]

_log = logging.getLogger(__name__)

# How long a *connected* session may take to shut down before it is cancelled.
# A session that never connected is cancelled almost immediately instead.
_CLOSE_TIMEOUT = 5.0
_CLOSE_TIMEOUT_NEVER_CONNECTED = 0.2

#: Description length per tool in the catalog, as OpenCode bounds it
#: (packages/core/src/codemode/catalog.ts:48).
_CATALOG_DESCRIPTION_LIMIT = 120

#: ~4 characters per token, so this is OpenCode's INLINE_BUDGET of 2,000 tokens
#: (catalog.ts:50).  Over it, configured servers are dropped, loudly.
_CATALOG_BUDGET_CHARS = 8_000


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


def build_mcp_catalog(
    servers: list[tuple[str, list[MCPToolHandle]]],
    budget_chars: int = _CATALOG_BUDGET_CHARS,
) -> str:
    """The catalog text: one entry per callable remote tool (M2).

    Pure, and deliberately so — the budget discipline is the whole reason this
    exists, and it is testable without a connection.  Config order is priority
    (the first server is the one the operator listed first); over budget, whole
    trailing servers are dropped and the drop is *said*, because silently
    invisible tools read as bugs to whoever debugged the model's call.
    """
    if not servers:
        return ""
    lines: list[str] = []
    used = 0
    dropped = 0
    for name, handles in servers:
        block: list[str] = [name]
        for handle in handles:
            description = (handle.description or "").strip().replace("\n", " ")
            if len(description) > _CATALOG_DESCRIPTION_LIMIT:
                description = description[:_CATALOG_DESCRIPTION_LIMIT] + "…"
            block.append(f"  {handle.name}: {description}" if description else f"  {handle.name}")
        text = "\n".join(block)
        if lines and used + len(text) + 1 > budget_chars:
            dropped += 1
            continue
        lines.append(text)
        used += len(text) + 1
    if dropped:
        lines.append(f"({dropped} more server(s) omitted: catalog budget)")
    header = (
        "<mcp-catalog>\n"
        "Remote tools, called with mcp_call(server, tool, input).\n"
        "server and tool must be spelled exactly as listed here.\n"
    )
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
        #: Which budget ran out, if one did: "startup" / "catalog" (M1).
        self.failed_phase: str | None = None
        self._factory = factory
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._serve(), name=f"mcp:{self.server.name}")

    async def _serve(self) -> None:
        """Connect, publish the tool list, hold the session, then close it here."""
        try:
            async with AsyncExitStack() as stack:
                self.session = await stack.enter_async_context(self._factory(self.server))
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
        async with stdio_client(_server_parameters(server)) as (read, write):
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
    """Connects the configured stdio MCP servers and returns their tools."""

    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        session_factory: SessionFactory | None = None,
        observer: Any | None = None,
    ) -> None:
        self._config = config or MCPConfig()
        self._session_factory = session_factory or _default_session_factory
        self._owners: list[_MCPSessionOwner] = []
        # R4a: an optional observer, so a server that never connects is *reported*
        # rather than only skipped (docs/r4-analysis.md 1.1, point 3).
        self._observer = observer

    async def open_all(self) -> None:
        """Connect every enabled server; a failure skips just that server.

        Returns nothing: M2 made :meth:`available` the single source for what
        can be called (the dispatcher and the catalog both read it), so letting
        this hand out a second list of tools would invite the two to drift.
        """
        if not self._config.enabled:
            return
        seen_servers: set[str] = set()
        for server in self._config.servers:
            if server.disabled or not server.name or not server.command:
                continue
            if server.name in seen_servers:
                # Two servers sharing a name produce identical tool prefixes, which
                # the registry rejects; keep the first and move on rather than
                # letting a config typo escape as a CLI traceback.
                continue
            seen_servers.add(server.name)
            owner = _MCPSessionOwner(server, self._session_factory)
            self._owners.append(owner)
            owner.start()
            ok, phase = await owner.wait_ready(
                server.timeout.startup / 1000, server.timeout.catalog / 1000
            )
            if not ok:
                if phase == "error":
                    reason = f"server '{server.name}': {owner.error!r}"
                elif phase == "catalog":
                    reason = (
                        f"server '{server.name}': tool listing did not finish "
                        f"within {server.timeout.catalog / 1000:g}s"
                    )
                else:
                    reason = (
                        f"server '{server.name}': did not become ready within "
                        f"{server.timeout.startup / 1000:g}s"
                    )
                report_degraded(
                    self._observer,
                    "mcp_server",
                    reason,
                    "its tools are not available this run",
                )

    def servers(self) -> list[dict[str, Any]]:
        """Every configured server and what became of it (MCP-MCP §2.1).

        Two sources are needed and neither is enough alone: ``_config.servers``
        is what the operator asked for, ``_owners`` is what happened.  A server
        skipped before an owner existed (no name, no command, a duplicate name)
        only exists in the config, and a server whose command is wrong only
        exists in ``_owners`` — reporting one side would make the other
        disappear from the dialog, which is worse than reporting a failure.

        ``ready`` is set on both the connected and the failed path, so success is
        "ready and no error" rather than "ready".
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
            elif owner is None:
                # No owner means open_all never even tried: name, command or a
                # duplicate name.  Say which, rather than leaving a blank row.
                if not server.name:
                    row["detail"] = "配置缺少 name"
                elif not server.command:
                    row["detail"] = "配置缺少 command"
                else:
                    row["detail"] = "与同名服务器重复，只连了第一个"
            elif owner.error is not None:
                # An exception can carry no message (``raise RuntimeError``), so
                # fall back to its class name rather than showing an empty row.
                row["detail"] = str(owner.error) or type(owner.error).__name__
            elif owner.failed_phase == "catalog":
                row["detail"] = (
                    f"工具列表未在 {server.timeout.catalog / 1000:g}s 内返回"
                )
            elif not owner.ready.is_set():
                row["detail"] = f"未在 {server.timeout.startup / 1000:g}s 内就绪"
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

    def available(self) -> list[tuple[str, list[MCPToolHandle]]]:
        """Connected servers and the handles the dispatcher may call (M2).

        Whitelist applies (an ``R7d`` filter is a budget decision, not a
        permission one), permissions deliberately do not: a tool the gate refuses
        must still be *listed* in the catalog, or its refusal reads as a bug.
        """
        rows: list[tuple[str, list[MCPToolHandle]]] = []
        # Owners, not the config list: a duplicate name is skipped before an
        # owner exists (open_all), so iterating the config would list the same
        # connected server once per config row.  The owner carries the config
        # it was built from, which is the one that matters.
        for owner in self._owners:
            if owner.error is not None or not owner.ready.is_set():
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
        """Why a *configured* server cannot be called, for the model's benefit.

        Deliberately says "reconnect from the panel": the one action that exists.
        """
        cfg = next((s for s in self._config.servers if s.name == server), None)
        if cfg is None:
            return None
        owner = self._owner(server)
        if owner is not None and owner.error is not None:
            detail = str(owner.error) or type(owner.error).__name__
            return (
                f"MCP server '{server}' is not connected: {detail}. "
                "Reconnect it from the MCP panel."
            )
        if owner is not None and owner.failed_phase is not None:
            return (
                f"MCP server '{server}' is not connected: it did not settle in "
                f"time. Reconnect it from the MCP panel."
            )
        if owner is None:
            if cfg.disabled:
                return f"MCP server '{server}' is disabled."
            if not cfg.command:
                return f"MCP server '{server}' has no command configured."
            return f"MCP server '{server}' is not connected. Reconnect it from the MCP panel."
        return None

    def _owner(self, name: str) -> _MCPSessionOwner | None:
        for owner in self._owners:
            if owner.server.name == name:
                return owner
        return None

    async def aclose(self) -> None:
        """Tear down every session opened by :meth:`open_all` (idempotent)."""
        owners, self._owners = self._owners, []
        for owner in owners:
            try:
                await owner.close()
            except Exception:  # noqa: BLE001 — shutting down must not raise
                _log.debug("MCP session %r failed to close", owner.server.name, exc_info=True)


__all__ = ["MCPManager", "MCPToolAdapter"]
