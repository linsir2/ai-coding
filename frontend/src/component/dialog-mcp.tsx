/**
 * MCP dialog (MCP-MCP §2.4) — read-only, and that is the point.
 *
 * The panel answers one question the old design could not: what became of every
 * MCP server in the config.  Read-only on purpose — our MCP is connected once
 * at startup, so a "reconnect" button would be a control with nothing behind
 * it (MCP-MCP §6).
 *
 * Each row's line is `status · tool count`; the command sits under it and the
 * failure reason (or the dropped tool names) takes the row's footer.
 * `DialogSelect` groups by category, so a bad server never sits among the
 * connected ones.  It has no per-row colour field, and inventing one to carry a
 * tone the text already names would be a contract with a single consumer.
 */

import { createSignal, onMount } from "solid-js"
import type { IpcClient } from "../ipc"
import type { SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"
import type { McpServer } from "../util/mcp"
import { mcpStatusLabel, mcpToolNote } from "../util/mcp"

/** The status word plus the tool count, which is the whole answer per row. */
const statusLine = (server: McpServer): string => {
  const note = mcpToolNote(server)
  return `${mcpStatusLabel(server.status)}${note ? ` · ${note}` : ""}`
}

export function DialogMcp(props: { client: IpcClient }) {
  const [rows, setRows] = createSignal<McpServer[]>([])
  const [error, setError] = createSignal("")

  onMount(async () => {
    try {
      const data = (await props.client.request("mcp.list")) as { servers?: McpServer[] }
      setRows(data.servers ?? [])
    } catch (cause) {
      setError(String(cause))
    }
  })

  const options = (): SelectOption[] =>
    rows().map((server) => ({
      title: server.name || "(未命名)",
      value: server.command + server.name,
      description: statusLine(server),
      // A failure's reason, or the tools the whitelist dropped — the reason is
      // what the operator needs, so it wins the footer over the command.
      footer: server.detail || server.command || undefined,
      // Grouped by "is anything wrong", not by "did it connect": a filtered
      // server did connect, it just brought fewer tools than it could have.
      category: server.status === "failed" ? "未连接" : "已连接",
    }))

  return (
    <DialogSelect
      title="MCP servers"
      options={options()}
      footer={error() || (rows().length === 0 ? "没有配置 MCP 服务器" : "esc close")}
    />
  )
}