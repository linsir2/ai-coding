/**
 * Permission panel (M5) — OpenCode's ``△ Permission required`` block, shown in
 * the prompt's slot while a WARN-tier tool waits for an answer.
 *
 * The answer travels back as the result of the server's ``approval.request``
 * (app.tsx registers the request handler).  ←/→ (or h/l/tab) pick, enter
 * confirms; y/a/n answer directly; esc rejects — the same fail-closed answer
 * the backend gives when a round-trip dies.
 */

import { TextAttributes } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { For, Show, createSignal } from "solid-js"
import { theme } from "../theme/tokens"
import { moveSelection } from "../util/dialog-select"
import type { PermissionDecision } from "../util/permission"
import { PERMISSION_OPTIONS } from "../util/permission"
import { toolTarget, toolVerb } from "../util/toolmeta"
import { PROMPT_BORDER } from "./prompt"

export interface PermissionRequest {
  toolName: string
  params: Record<string, unknown>
  reason: string
}

export function PermissionPanel(props: {
  request: PermissionRequest
  onDecide: (decision: PermissionDecision) => void
}) {
  const [selected, setSelected] = createSignal(0)
  const move = (delta: number) =>
    setSelected((current) => moveSelection(current, delta, PERMISSION_OPTIONS.length))
  const confirm = () => {
    const option = PERMISSION_OPTIONS[selected()]
    if (option) props.onDecide(option.decision)
  }

  useBindings(() => ({
    enabled: true,
    commands: [
      { name: "permission.prev", desc: "Previous option", run: () => move(-1) },
      { name: "permission.next", desc: "Next option", run: () => move(1) },
      { name: "permission.confirm", desc: "Confirm the selected option", run: confirm },
      { name: "permission.once", desc: "Allow this call", run: () => props.onDecide("yes") },
      { name: "permission.always", desc: "Allow and remember", run: () => props.onDecide("always") },
      { name: "permission.reject", desc: "Reject this call", run: () => props.onDecide("no") },
    ],
    bindings: [
      { key: "left", cmd: "permission.prev" },
      { key: "h", cmd: "permission.prev" },
      { key: "right", cmd: "permission.next" },
      { key: "l", cmd: "permission.next" },
      { key: "tab", cmd: "permission.next" },
      { key: "return", cmd: "permission.confirm" },
      { key: "y", cmd: "permission.once" },
      { key: "a", cmd: "permission.always" },
      { key: "n", cmd: "permission.reject" },
      { key: "escape", cmd: "permission.reject" },
    ],
  }))

  const target = () => toolTarget(props.request.toolName, props.request.params)

  return (
    <box
      width="100%"
      flexDirection="column"
      border={["left", "bottom"]}
      customBorderChars={PROMPT_BORDER}
      borderColor={theme.actionPrimaryFocused}
      backgroundColor={theme.backgroundBase}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
    >
      <text fg={theme.feedbackWarning} attributes={TextAttributes.BOLD}>
        △ Permission required
      </text>
      <text fg={theme.textBase}>
        {`${toolVerb(props.request.toolName)}${target() ? ` · ${target()}` : ""}`}
      </text>
      <Show when={props.request.reason}>
        <text fg={theme.textMuted}>{props.request.reason}</text>
      </Show>
      <box flexDirection="row" gap={2} marginTop={1}>
        <For each={PERMISSION_OPTIONS}>
          {(option, index) => (
            <box
              paddingLeft={1}
              paddingRight={1}
              backgroundColor={
                index() === selected() ? theme.actionPrimaryFocused : theme.backgroundRaisedHigh
              }
            >
              <text
                fg={index() === selected() ? theme.backgroundBase : theme.textBase}
                attributes={index() === selected() ? TextAttributes.BOLD : undefined}
              >
                {option.label}
              </text>
            </box>
          )}
        </For>
      </box>
      <text fg={theme.textMuted}>←/→ select · enter confirm · esc reject</text>
    </box>
  )
}
