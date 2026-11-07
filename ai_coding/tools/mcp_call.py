"""``mcp_call`` — the one tool the model sees for every MCP server (M2).

One schema instead of one per remote tool, because a schema per tool is re-sent
on every model step: 13 tools measured 13,301 characters per step
(docs/features.md 538-541).  What the model reads instead is the catalog
(:func:`ai_coding.core.mcp.build_mcp_catalog`), regenerated per step from live
manager state and carried in the loop's volatile tail.

Two names are deliberately different things here.  The registry name is
``mcp_call`` (one vessel); the *authorized* name is ``fs__write_file``
(:meth:`permission_name`), which is what the gate judges and what the approval
prompt shows — otherwise the operator is asked to approve "mcp_call" with no
idea which remote call it carries.  Catalog visibility and execution
authorization stay separate: a tool the gate refuses is still listed, which is
what makes its refusal legible.
"""

from __future__ import annotations

from typing import Any

from ai_coding.core.mcp import MCPManager, invoke_remote_tool
from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {"type": "string", "description": "server name, from the mcp-catalog"},
        "tool": {"type": "string", "description": "tool name, spelled exactly as listed"},
        "input": {"type": "object", "description": "the tool's own arguments"},
    },
    "required": ["server", "tool", "input"],
}


class MCPCallTool(BaseTool):
    """Calls one remote tool on one connected server."""

    # Remote side effects are unknown, so never run these concurrently.
    parallel_safe = False

    def __init__(self, manager: MCPManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "mcp_call"

    @property
    def origin(self) -> str:
        return "mcp"

    @property
    def description(self) -> str:
        return (
            "Call a tool on a connected MCP server. The available servers and "
            "their tools are listed in the mcp-catalog block; spell both names "
            "exactly as listed there."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return dict(_PARAMS_SCHEMA)

    def write_paths_for(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> tuple[str, ...]:
        """The remote side's writes are opaque: claim the whole workspace."""
        return ()

    def permission_name(self, params: dict[str, Any] | None = None) -> str:
        """What the gate judges: the real remote call, not the vessel.

        A pair that is not callable is judged as ``mcp_call`` itself (which the
        gate allows) and comes back from :meth:`execute` as a result naming the
        real ones — a hallucination is the model's error to read, not an
        exception, and not something to prompt the operator about.
        """
        params = params or {}
        server = str(params.get("server") or "")
        tool = str(params.get("tool") or "")
        if server and tool and self._manager.lookup(server, tool) is not None:
            return f"{server}__{tool}"
        return self.name

    async def execute(
        self,
        params: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        server = str(params.get("server") or "")
        tool = str(params.get("tool") or "")
        arguments = params.get("input")
        if not isinstance(arguments, dict):
            arguments = {}

        rows = self._manager.available()
        connected = [name for name, _handles in rows]
        if server not in connected:
            # Prefer the specific reason over a generic one: "it failed to
            # connect and why" is the message that lets the model tell the user
            # something actionable.
            unreachable = self._manager.unreachable_reason(server)
            reason = unreachable
            if reason is None:
                reason = (
                    f"no MCP server named '{server}' is configured. "
                    f"Connected servers: {', '.join(connected) or '(none)'}."
                )
            # MCP-07 keeps three leaves: a configured-but-unreachable server is
            # a transport-shaped failure, while a hallucinated name is the
            # model's error (and stays retryable, which is what it was before —
            # NOT_FOUND would have made a retry look forbidden).
            code = "mcp_transport" if unreachable else "mcp_tool_error"
            return self._error(reason, server, tool, code)

        handle = self._manager.lookup(server, tool)
        if handle is None:
            tools = ", ".join(
                h.name for name, handles in rows if name == server for h in handles
            )
            return self._error(
                f"MCP server '{server}' has no tool '{tool}'. Its tools: {tools}.",
                server,
                tool,
                "mcp_tool_error",
            )

        # MCP-03: an execution bound is what makes a timeout possible at all.
        # With ``execution`` unset (the default) or zero there is no session
        # retirement, so a zero budget cannot retire a healthy session on
        # every single call.
        timeout = handle.execution_timeout
        on_session_stale = (
            (lambda: self._manager.note_session_stale(server))
            if timeout is not None and timeout > 0
            else None
        )

        return await invoke_remote_tool(
            server=server,
            tool_name=tool,
            session=handle.session,
            arguments=arguments,
            ctx=ctx,
            execution_timeout=timeout,
            progress_name=f"{server}__{tool}",
            # A parked owner never sees its subprocess die, so this call is the
            # only witness: a dead transport marks the server and wakes its
            # supervisor (M3 §1).  An ordinary tool error is not reported here
            # — the kernel only calls this for the transport-class ones.
            on_transport_error=lambda: self._manager.note_transport_death(server),
            on_session_stale=on_session_stale,
        )

    def _error(
        self, message: str, server: str, tool: str, code: str = "mcp_tool_error"
    ) -> ToolResult:
        """A model-facing failure: named, classified, and never an exception."""
        return ToolResult(
            success=False,
            output=f"error: {message}",
            error=code,
            data={"server": server, "tool": tool},
        )


__all__ = ["MCPCallTool"]
