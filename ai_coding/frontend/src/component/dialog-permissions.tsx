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
import type { GrantRecord, PermissionsState } from "../util/permissions-dialog"
import {
  RESET_SESSION_COMMAND,
  grantLine,
  layerSummary,
  moveGrantCursor,
  policyLabel,
  revokeGrantCommand,
  ruleLine,
  shortStorePath,
  toggleCommand,
} from "../util/permissions-dialog"
export function DialogPermissions(props: { client: IpcClient; liveSessionId: string }) {
  const [permissions, setPermissions] = createSignal<PermissionsState | null>(null)
  const [error, setError] = createSignal("")
  const [cursor, setCursor] = createSignal(0)

  const grants = (): GrantRecord[] => permissions()?.grants ?? []
  const cursorGrant = (): GrantRecord | null => grants()[cursor()] ?? null

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

  const move = (delta: number) => {
    setCursor((current) => moveGrantCursor(current, delta, grants().length))
  }

  /** M-D3: forget the grant under the cursor, through the backend's own command. */
  const revoke = async () => {
    const grant = cursorGrant()
    if (!grant || !grant.key) return
    await dispatch(revokeGrantCommand(grant.key))
    setCursor(0)
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
      {
        name: "dialog.permissions.prev",
        desc: "Previous grant",
        run: () => move(-1),
      },
      {
        name: "dialog.permissions.next",
        desc: "Next grant",
        run: () => move(1),
      },
      {
        name: "dialog.permissions.revoke",
        desc: "Revoke the selected grant",
        run: () => void revoke(),
      },
    ],
    bindings: [
      { key: "p", cmd: "dialog.permissions.toggle" },
      { key: "c", cmd: "dialog.permissions.clear" },
      { key: "r", cmd: "dialog.permissions.reset" },
      { key: "up", cmd: "dialog.permissions.prev" },
      { key: "down", cmd: "dialog.permissions.next" },
      { key: "x", cmd: "dialog.permissions.revoke" },
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
                  {/* A4 follow-up: one readable line, not the raw layer table
                      with zeros ("base 83 · standing 0 · agent 7 · …"). */}
                  <box flexDirection="row" justifyContent="space-between">
                    <text fg={theme.textMuted}>Rules</text>
                    <text fg={theme.textBase}>{layerSummary(rules().layers)}</text>
                  </box>
                  <Show when={rules().session.length > 0}>
                    <text fg={theme.textMuted}>  this session</text>
                    <For each={rules().session}>
                      {(rule) => (
                        <box flexDirection="row" justifyContent="space-between">
                          <text fg={theme.textMuted}>{"  "}</text>
                          <text fg={theme.textBase}>{ruleLine(rule)}</text>
                        </box>
                      )}
                    </For>
                  </Show>
                  <For each={rules().errors}>
                    {(error) => <text fg={theme.textMuted}>{`  ! ${error}`}</text>}
                  </For>
                </>
              )}
            </Show>
            <text fg={theme.textMuted}>{`Remembered  ${grants().length}`}</text>
            <For each={grants()}>
              {(grant, index) => (
                <text fg={index() === cursor() ? theme.textBase : theme.textMuted}>
                  {`  ${index() === cursor() ? "▸" : " "} ${grantLine(grant)}`}
                </text>
              )}
            </For>
            <Show when={grants().length === 0}>
              <text fg={theme.textMuted}>{"  none yet — Allow always adds one"}</text>
            </Show>
            <Show when={state().onceGrants.length > 0}>
              <text fg={theme.textMuted}>{`Once  ${state().onceGrants.length}`}</text>
              <For each={state().onceGrants}>
                {(grant) => (
                  <text fg={theme.textMuted}>{`    ${grantLine({ key: grant, expiresAt: null })}`}</text>
                )}
              </For>
            </Show>
            <Show when={state().storePath}>
              <text fg={theme.textMuted}>{`Storage  ${shortStorePath(state().storePath)}`}</text>
            </Show>
          </>
        )}
      </Show>
      <text fg={theme.textMuted}>
        {error() || "↑↓ grant · x revoke · p policy · c forget · r reset · esc close"}
      </text>
    </box>
  )
}