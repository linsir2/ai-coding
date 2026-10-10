/**
 * MCP dialog (MCP-MCP §2.4) — what became of every configured server.
 *
 * It used to be read-only, and that was the point: our MCP connected once at
 * startup, so a "reconnect" button would have been a control with nothing
 * behind it.  M3 changed that — a supervisor per server retries forever, and
 * `mcp.connect` wakes one now — so the failed row's enter key is that control.
 * It is the only action: a connected server has nothing to ask it, and a
 * disabled one cannot be connected at all.
 *
 * Each row's line is `status · tool count`; the command sits under it and the
 * failure reason (or the dropped tool names) takes the row's footer.
 * `DialogSelect` groups by category, so a bad server never sits among the
 * connected ones.  It has no per-row colour field, and inventing one to carry a
 * tone the text already names would be a contract with a single consumer.
 *
 * Rows arrive on open and again on every `mcp.status.changed` (the `revision`
 * signal the app owns), because a supervisor reconnecting in the background
 * changes what this list should say without anyone touching the panel.
 */

import { createEffect, createSignal, onMount } from "solid-js"
import type { IpcClient } from "../ipc"
import type { SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"
import type { McpServer } from "../util/mcp"
import { mcpRowCanConnect, mcpStatusLabel, mcpToolNote } from "../util/mcp"

/** The status word plus the tool count, which is the whole answer per row. */
const statusLine = (server: McpServer): string => {
  const note = mcpToolNote(server)
  return `${mcpStatusLabel(server.status)}${note ? ` · ${note}` : ""}`
}

export function DialogMcp(props: {
  client: IpcClient
  /** Bumped on every `mcp.status.changed`; the panel re-reads its rows. */
  revision?: () => number
}) {
  const [rows, setRows] = createSignal<McpServer[]>([])
  const [error, setError] = createSignal("")

  const refresh = async (): Promise<void> => {
    try {
      const data = (await props.client.request("mcp.list")) as { servers?: McpServer[] }
      setRows(data.servers ?? [])
    } catch (cause) {
      setError(String(cause))
    }
  }

  onMount(refresh)
  // The event arrives while the panel is open; the initial fetch above covers
  // the state it was opened in, so a revision of 0 is deliberately not a fetch.
  createEffect(() => {
    const revision = props.revision?.() ?? 0
    if (revision > 0) void refresh()
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
      // The one action the panel has (M3): a failed row can be retried now.
      onSelect: mcpRowCanConnect(server.status)
        ? () => void props.client.request("mcp.connect", { server: server.name })
        : undefined,
    }))

  return (
    <DialogSelect
      title="MCP servers"
      options={options()}
      footer={error() || (rows().length === 0 ? "没有配置 MCP 服务器" : "esc close")}
    />
  )
}
