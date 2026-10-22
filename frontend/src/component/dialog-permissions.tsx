/**
 * Permissions dialog (A4) — the app's one home for the approval policy.
 *
 * The policy is workspace-global (`security/permission_gate.py` gates every
 * session under this workspace), so it does not belong to a session-scoped
 * panel.  `liveSessionId` is named for what it is: the transport the command
 * travels on (`session.command` requires a live session id), not "this dialog's
 * session" — the dialog itself has none.
 *
 * The switches stay commands rather than becoming a second write path: the
 * dispatcher owns the change and re-arms the approval provider on the way out
 * (A3 J2/I1).  Read side is `permissions.state`; write side is unchanged.
 */

import { TextAttributes } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { For, Show, createSignal, onMount } from "solid-js"
import type { IpcClient } from "../ipc"
import { theme } from "../theme/tokens"
import type { PermissionsState } from "../util/permissions-dialog"
import {
  RESET_SESSION_COMMAND,
  grantLine,
  policyLabel,
  ruleLine,
  toggleCommand,
} from "../util/permissions-dialog"

export function DialogPermissions(props: { client: IpcClient; liveSessionId: string }) {
  const [permissions, setPermissions] = createSignal<PermissionsState | null>(null)
  const [error, setError] = createSignal("")

  const read = async () => {
    try {
      setPermissions((await props.client.request("permissions.state")) as PermissionsState)
      setError("")
    } catch (cause) {
      setError(String(cause))
    }
  }

  const dispatch = async (command: string) => {
    try {
      await props.client.request("session.command", { id: props.liveSessionId, text: command })
    } catch (cause) {
      setError(String(cause))
      return
    }
    await read()
  }

  onMount(read)

  useBindings(() => ({
    enabled: permissions() !== null,
    commands: [
      {
        name: "dialog.permissions.toggle",
        desc: "Switch the approval policy",
        run: () => void dispatch(toggleCommand(permissions()!.policy)),
      },
      {
        name: "dialog.permissions.clear",
        desc: "Forget remembered grants",
        run: () => void dispatch("/permissions clear"),
      },
      {
        name: "dialog.permissions.reset",
        desc: "Clear this session's rules",
        run: () => void dispatch(RESET_SESSION_COMMAND),
      },
    ],
    bindings: [
      { key: "p", cmd: "dialog.permissions.toggle" },
      { key: "c", cmd: "dialog.permissions.clear" },
      { key: "r", cmd: "dialog.permissions.reset" },
    ],
  }))

  return (
    <box flexDirection="column" width="100%" paddingLeft={4} paddingRight={4} paddingBottom={1} gap={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
          Permissions
        </text>
        <text fg={theme.textMuted}>esc</text>
      </box>
      <Show when={permissions()} fallback={<text fg={theme.textMuted}>reading…</text>}>
        {(state) => (
          <>
            <box flexDirection="row" justifyContent="space-between">
              <text fg={theme.textMuted}>Policy</text>
              <text fg={theme.textBase}>{policyLabel(state().policy)}</text>
            </box>
            <Show when={state().rules}>
              {(rules) => (
                <>
                  <text fg={theme.textMuted}>
                    {Object.entries(rules().layers)
                      .map(([layer, count]) => `${layer} ${count}`)
                      .join(" · ")}
                  </text>
                  <For each={rules().session}>
                    {(rule) => (
                      <box flexDirection="row" justifyContent="space-between">
                        <text fg={theme.textMuted}>{"  session"}</text>
                        <text fg={theme.textBase}>{ruleLine(rule)}</text>
                      </box>
                    )}
                  </For>
                  <For each={rules().errors}>
                    {(error) => <text fg={theme.textMuted}>{`  ! ${error}`}</text>}
                  </For>
                </>
              )}
            </Show>
            <For each={state().grants.length > 0 ? state().grants : [""]}>
              {(grant) => (
                <box flexDirection="row" justifyContent="space-between">
                  <text fg={theme.textMuted}>{"  remembered"}</text>
                  <text fg={theme.textBase}>{grantLine(grant)}</text>
                </box>
              )}
            </For>
            <Show when={state().storePath}>
              <box flexDirection="row" justifyContent="space-between">
                <text fg={theme.textMuted}>{"  store"}</text>
                <text fg={theme.textBase}>{state().storePath}</text>
              </box>
            </Show>
          </>
        )}
      </Show>
      <text fg={theme.textMuted}>
        {error() || "p switch policy · c forget grants · r reset session rules · esc close"}
      </text>
    </box>
  )
}