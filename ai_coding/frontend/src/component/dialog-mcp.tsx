/**
 * MCP dialog (MCP-MCP §2.4) — what became of every configured server.
 *
 * It used to be read-only, and that was the point: our MCP connected once at
 * startup, so a "reconnect" button would have been a control with nothing
 * behind it.  M3 changed that — a supervisor per server retries forever, and
 * `mcp.connect` wakes one now — so the failed row's enter key is that control.
 * A connected server has nothing to ask it, and a disabled one cannot be
 * connected at all.  M5 added the panel's second action, `ctrl+a`: the guided
 * connect wizard, because "let me add one" has to start somewhere, and the
 * panel that lists servers is the somewhere.
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
import type { SelectAction } from "../util/dialog-select"
import type { IpcClient } from "../ipc"
import type { SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"
import type { McpServer } from "../util/mcp"
import {
  mcpRowCanConnect,
  mcpRowCanLogin,
  mcpStatusLabel,
  mcpToolNote,
} from "../util/mcp"

/** The status word plus the tool count, which is the whole answer per row. */
const statusLine = (server: McpServer): string => {
  const note = mcpToolNote(server)
  return `${mcpStatusLabel(server.status)}${note ? ` · ${note}` : ""}`
}

export function DialogMcp(props: {
  client: IpcClient
  /** Bumped on every `mcp.status.changed`; the panel re-reads its rows. */
  revision?: () => number
  /** Opens the guided connect wizard (M5); the app wires the dialog itself. */
  onConnectNew?: () => void
}) {
  const [rows, setRows] = createSignal<McpServer[]>([])
  const [error, setError] = createSignal("")
  // The master switch, which decides whether the wizard is even worth
  // opening: `mcp.enabled: false` is the shipped default, so without this the
  // panel cheerfully offers "connect" and the wizard collects three answers
  // before the backend refuses.
  const [enabled, setEnabled] = createSignal(true)

  const refresh = async (): Promise<void> => {
    try {
      const data = (await props.client.request("mcp.list")) as {
        servers?: McpServer[]
        enabled?: boolean
      }
      setRows(data.servers ?? [])
      setEnabled(data.enabled !== false)
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
      // The panel's actions, one per waiting state (M3/M4): a failed row
      // retries, a needs_auth row signs in.  Both are "the operator did
      // something"; nothing else offers an action.  (M4: the arrow in
      // "Sign in required →" is literal — enter starts it.)
      onSelect: mcpRowCanLogin(server.status)
        ? () => void props.client.request("mcp.login", { server: server.name })
        : mcpRowCanConnect(server.status)
          ? () => void props.client.request("mcp.connect", { server: server.name })
          : undefined,
    }))

  // A ctrl-combo, not a bare letter: the panel's search input has focus, so a
  // plain "a" would be typed into the filter instead of reaching the binding
  // (the same reason the sessions panel's delete is ctrl+d).
  const actions = (): readonly SelectAction[] =>
    props.onConnectNew && enabled()
      ? [{ key: "ctrl+a", title: "connect", onTrigger: props.onConnectNew }]
      : []

  return (
    <DialogSelect
      title="MCP servers"
      options={options()}
      actions={actions()}
      footer={
        error() ||
        [
          rows().length === 0 ? "没有配置 MCP 服务器" : "",
          enabled()
            ? props.onConnectNew
              ? "ctrl+a connect a new server"
              : ""
            : "MCP 已关闭（配置里 mcp.enabled: false）· 先打开它才能接入",
          "esc close",
        ]
          .filter(Boolean)
          .join(" · ")
      }
    />
  )
}
