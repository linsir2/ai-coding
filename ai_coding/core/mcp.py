"""MCP integration over the ``mcp`` package directly (R1a-6).

Replaces the M5 implementation, which produced ``agents.mcp.MCPServerStdio``
objects for the OpenAI Agents SDK.  Now the manager connects stdio servers
itself and exposes each remote tool as an ordinary ``BaseTool`` in the same
registry the dispatcher uses — so MCP tools go through the same permission gate
and the same approval path as local ones.

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


class MCPToolAdapter(BaseTool):
    """Exposes one remote MCP tool as a local ``BaseTool``."""

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
        # Seconds (None = only the turn deadline bounds this call).  Comes from
        # ``timeout.execution`` (ms) converted at the manager.
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
        tool_name = str(getattr(self._tool, "name", ""))
        # R8c-S3: a server that reports progress gets shown like a local tool's
        # stdout.  Servers that never call report_progress simply emit nothing.
        live = resolve_ctx(ctx)
        steps = itertools.count()

        def _progress(
            progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            body = message or (f"{progress}/{total}" if total else str(progress))
            live.emit(
                ToolChunk(
                    call_id=live.call_id,
                    tool_name=self.name,
                    stream="progress",
                    text=str(body),
                    seq=next(steps),
                )
            )

        try:
            if self._execution_timeout is not None:
                result = await run_with_deadline(
                    self._session.call_tool(
                        tool_name, params, progress_callback=_progress
                    ),
                    timeout=self._execution_timeout,
                    label=f"mcp_call:{tool_name}",
                )
            else:
                result = await self._session.call_tool(
                    tool_name, params, progress_callback=_progress
                )
        except DeadlineExpired:
            return ToolResult(
                success=False,
                output=f"error: MCP tool '{tool_name}' did not answer within "
                f"{self._execution_timeout:g}s",
                error="mcp_error",
                data={"server": self._server, "tool": tool_name},
            )
        except Exception as exc:  # noqa: BLE001 — tool boundary
            return ToolResult(
                success=False,
                output=f"error: MCP tool '{tool_name}' failed: {exc}",
                error="mcp_error",
                data={"server": self._server, "tool": tool_name},
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
                "server": self._server,
                "tool": tool_name,
                "is_error": is_error,
                "structured": getattr(result, "structured_content", None),
            },
            error="mcp_error" if is_error else None,
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

    async def open_all(self) -> list[BaseTool]:
        """Connect every enabled server; a failure skips just that server."""
        if not self._config.enabled:
            return []
        tools: list[BaseTool] = []
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
                continue
            # R7d: register only what the user asked for.  Every tool schema is
            # re-sent on every model step, so this is the one knob that lowers the
            # per-step cost of a talkative server (docs/r7-analysis.md 1.2).
            wanted = {str(name) for name in getattr(server, "tools", []) or []}
            execution_s = (
                server.timeout.execution / 1000
                if server.timeout.execution is not None
                else None
            )
            for tool in owner.tools:
                remote = str(getattr(tool, "name", ""))
                if wanted and remote not in wanted:
                    continue
                tools.append(
                    MCPToolAdapter(
                        server=server.name,
                        tool=tool,
                        session=owner.session,
                        execution_timeout=execution_s,
                    )
                )
        return tools

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

    async def aclose(self) -> None:
        """Tear down every session opened by :meth:`open_all` (idempotent)."""
        owners, self._owners = self._owners, []
        for owner in owners:
            try:
                await owner.close()
            except Exception:  # noqa: BLE001 — shutting down must not raise
                _log.debug("MCP session %r failed to close", owner.server.name, exc_info=True)


__all__ = ["MCPManager", "MCPToolAdapter"]
