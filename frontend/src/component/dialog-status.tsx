/**
 * Status dialog (M3, permissions folded in A3.1) — the app's config panel:
 * `config.summary` plus the session/model/workspace facts, plus the approval
 * policy and the remembered grants.
 *
 * Permissions live here rather than in a dialog of their own because that is
 * where OpenCode keeps them (`dialog-config.tsx`: one entry, two values,
 * ``prompt``/``autoaccept`` — our ``confirm``/``auto`` are the same pair under
 * the dispatcher's names).  Read-only view; the switches dispatch the
 * backend's own commands, so the provider re-arm (I1) keeps one home.
 */

import { TextAttributes } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { For, Show, createSignal, onMount } from "solid-js"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import { theme } from "../theme/tokens"
import type { ApprovalPolicy, PermissionsState } from "../util/permissions-dialog"
import { grantLine, policyLabel, toggleCommand } from "../util/permissions-dialog"

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
  const dialog = useDialog()
  const [rows, setRows] = createSignal<[string, string][]>([])
  const [permissions, setPermissions] = createSignal<PermissionsState | null>(null)
  const [error, setError] = createSignal("")

  const readPermissions = async () => {
    try {
      setPermissions((await props.client.request("permissions.state")) as PermissionsState)
    } catch {
      /* the rest of the panel still stands; the policy row just stays hidden */
    }
  }

  // The switches are commands, not a second mutation path: the dispatcher owns
  // the change and re-arms the provider on the way out (J2/I1).
  const dispatch = async (command: string) => {
    await props.client.request("session.command", { id: props.sessionId, text: command })
    await readPermissions()
  }

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
    await readPermissions()
  })

  useBindings(() => ({
    enabled: permissions() !== null,
    commands: [
      {
        name: "dialog.status.permissions.toggle",
        desc: "Switch the approval policy",
        run: () => void dispatch(toggleCommand(permissions()!.policy)),
      },
      {
        name: "dialog.status.permissions.clear",
        desc: "Forget remembered grants",
        run: () => void dispatch("/permissions clear"),
      },
    ],
    bindings: [
      { key: "p", cmd: "dialog.status.permissions.toggle" },
      { key: "c", cmd: "dialog.status.permissions.clear" },
    ],
  }))

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
      <Show when={permissions()}>
        {(state) => (
          <>
            <text fg={theme.textMuted}>{"─".repeat(40)}</text>
            <box flexDirection="row" justifyContent="space-between">
              <text fg={theme.textMuted}>Permissions</text>
              <text fg={theme.textBase}>{policyLabel(state().policy)}</text>
            </box>
            <For each={state().grants.length > 0 ? state().grants : [""]}>
              {(grant) => (
                <box flexDirection="row" justifyContent="space-between">
                  <text fg={theme.textMuted}>{"  remembered"}</text>
                  <text fg={theme.textBase}>{grantLine(grant)}</text>
                </box>
              )}
            </For>
          </>
        )}
      </Show>
      <text fg={theme.textMuted}>
        {error() || "p switch policy · c forget grants · esc close"}
      </text>
    </box>
  )
}
