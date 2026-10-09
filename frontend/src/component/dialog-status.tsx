/**
 * Status dialog (M3) — a read-only key/value panel.  Real data only:
 * `config.summary` plus the session/model/workspace facts the app already has.
 * Not a DialogSelect: there is nothing to select.
 */

import { TextAttributes } from "@opentui/core"
import { For, createSignal, onMount } from "solid-js"
import type { IpcClient } from "../ipc"
import { theme } from "../theme/tokens"

interface ConfigSummary {
  maxContextTokens: number
  maxToolIterations: number
  stopPolicy: string
}

export function DialogStatus(props: {
  client: IpcClient
  sessionTitle: string
  sessionId: string
  provider: string
  model: string
  workspace: string
}) {
  const [rows, setRows] = createSignal<[string, string][]>([])
  const [error, setError] = createSignal("")

  onMount(async () => {
    try {
      const data = (await props.client.request("config.summary")) as ConfigSummary
      setRows([
        ["Session", `${props.sessionTitle} · ${props.sessionId.slice(0, 8)}`],
        ["Model", [props.provider, props.model].filter(Boolean).join(" · ")],
        ["Workspace", props.workspace],
        ["Max context tokens", String(data.maxContextTokens)],
        ["Max tool iterations", String(data.maxToolIterations)],
        ["Stop policy", data.stopPolicy],
      ])
    } catch (cause) {
      setError(String(cause))
    }
  })

  return (
    <box flexDirection="column" width="100%" paddingLeft={4} paddingRight={4} paddingBottom={1} gap={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
          Status
        </text>
        <text fg={theme.textMuted}>esc</text>
      </box>
      <box flexDirection="column">
        <For each={rows()}>
          {(row) => (
            <box flexDirection="row" justifyContent="space-between">
              <text fg={theme.textMuted}>{row[0]}</text>
              <text fg={theme.textBase}>{row[1]}</text>
            </box>
          )}
        </For>
      </box>
      <text fg={theme.textMuted}>{error() || "esc close"}</text>
    </box>
  )
}
