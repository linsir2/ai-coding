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
import { useTerminalDimensions } from "@opentui/solid"
import { For, Show, createSignal } from "solid-js"
import { theme } from "../theme/tokens"
import { moveSelection } from "../util/dialog-select"
import type { PermissionDecision, PermissionRequest } from "../util/permission"
import { NO_GRANT_NOTE, permissionOptions, permissionPayload } from "../util/permission"
import { toolTarget, toolVerb } from "../util/toolmeta"
import { PROMPT_BORDER } from "./prompt"

export function PermissionPanel(props: {
  request: PermissionRequest
  /** `note` rides only with a refusal: it is what the operator would accept
   *  instead (C-3). The panel owns that rule, so the backend never has to. */
  onDecide: (decision: PermissionDecision, note?: string) => void
}) {
  const dimensions = useTerminalDimensions()
  const [selected, setSelected] = createSignal(0)
  // P0-4A: the table shrinks when the gate could not remember the call, so the
  // index, the arrow wrap and the rendered row all read the same list.
  const options = () => permissionOptions(props.request)
  const move = (delta: number) =>
    setSelected((current) => moveSelection(current, delta, options().length))
  const confirm = () => {
    // A new request can arrive while the panel is open (app.tsx supersedes a
    // stale one), and the index may then point past the shorter table.
    const option = options()[selected()] ?? options()[options().length - 1]
    if (!option) return
    // The note belongs to the refusal the operator just typed it into; every
    // other answer is a decision about this call, not a sentence about the next.
    props.onDecide(option.decision, refusalSelected() ? note() : undefined)
  }
  const grantable = () => props.request.grantable !== false
  // C-3: the note row belongs to the refusal option. It is not decoration on
  // top of the panel: a focused <input> does NOT swallow letter keys (measured
  // live -- with plan-review's input focused, "1" still ran its approve
  // command), so a row that appeared unconditionally would turn "sleep" into
  // "stop this turn" and "no thanks" into an immediate refusal.
  const [note, setNote] = createSignal("")
  const refusalSelected = () => options()[selected()]?.decision === "no"

  useBindings(() => ({
    enabled: true,
    commands: [
      { name: "permission.prev", desc: "Previous option", run: () => move(-1) },
      { name: "permission.next", desc: "Next option", run: () => move(1) },
      { name: "permission.confirm", desc: "Confirm the selected option", run: confirm },
      { name: "permission.once", desc: "Allow this call", run: () => props.onDecide("yes") },
      { name: "permission.reject", desc: "Reject this call", run: () => props.onDecide("no") },
      { name: "permission.stop", desc: "Reject and end this turn", run: () => props.onDecide("stop") },
      // Only reachable while the option is on screen: a keymap that still binds
      // this command must not manufacture the decision the panel just hid.
      ...(grantable()
        ? [
            {
              name: "permission.always",
              desc: "Allow and remember",
              run: () => props.onDecide("always"),
            },
          ]
        : []),
    ],
    bindings: refusalSelected()
      ? // The note row is up: the keyboard belongs to the text. Only the
        // navigation keys and the fail-closed escape stay -- arrows move the
        // selection (and therefore close the row), escape still refuses.
        [
          { key: "left", cmd: "permission.prev" },
          { key: "right", cmd: "permission.next" },
          { key: "tab", cmd: "permission.next" },
          { key: "escape", cmd: "permission.reject" },
        ]
      : [
          { key: "left", cmd: "permission.prev" },
          { key: "h", cmd: "permission.prev" },
          { key: "right", cmd: "permission.next" },
          { key: "l", cmd: "permission.next" },
          { key: "tab", cmd: "permission.next" },
          { key: "return", cmd: "permission.confirm" },
          { key: "y", cmd: "permission.once" },
          { key: "n", cmd: "permission.reject" },
          { key: "s", cmd: "permission.stop" },
          { key: "escape", cmd: "permission.reject" },
          ...(grantable() ? [{ key: "a", cmd: "permission.always" }] : []),
        ],
  }))

  const target = () => toolTarget(props.request.toolName, props.request.params)
  const payload = () => permissionPayload(props.request)
  // The panel is anchored bottom-left, so the terminal width is the budget; the
  // block is a glance, not a diff, and a long line must not wrap into the
  // answer keys.
  const panelWidth = () => Math.max(40, dimensions().width - 8)

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
      {/* What the call will actually do. An MCP call's own line can only echo
          ``fs__write_file`` — the arguments arrived with the request and were
          never printed, so this is what turns "approve?" into an informed
          answer. */}
      <For each={payload()}>
        {(line) => (
          <text fg={theme.textMuted}>
            {line.length > panelWidth() - 4 ? `  ${line.slice(0, panelWidth() - 7)}…` : `  ${line}`}
          </text>
        )}
      </For>
      <Show when={props.request.reason}>
        <text fg={theme.textMuted}>{props.request.reason}</text>
      </Show>
      {/* P0-4A: say why the remembered option is absent rather than silently
          showing fewer choices -- an operator who cannot tell "bash is never
          remembered" from "the panel lost an option" will press the wrong key. */}
      <Show when={!grantable()}>
        <text fg={theme.textMuted}>
          {NO_GRANT_NOTE.length > panelWidth() - 4
            ? `${NO_GRANT_NOTE.slice(0, panelWidth() - 7)}…`
            : NO_GRANT_NOTE}
        </text>
      </Show>
      {/* C-3: the note row. Same shape as plan-review's feedback input (the
          repo's one precedent for a human typing into a card), reached by
          selecting the refusal instead of pressing its quick key. */}
      <Show when={refusalSelected()}>
        <box flexDirection="row" marginTop={1}>
          <text fg={theme.textMuted}>{"› "}</text>
          <input
            focused
            placeholder="为什么拒绝？改做什么也行，enter 提交（可留空）"
            onInput={(value: string) => setNote(value)}
            onSubmit={confirm}
          />
        </box>
      </Show>
      <box flexDirection="row" gap={2} marginTop={1}>
        <For each={options()}>
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
      <text fg={theme.textMuted}>
        {refusalSelected()
          ? "enter 附言拒绝 · ←/→ 换个选项 · esc 直接拒绝"
          : "←/→ select · enter confirm · s stop · esc reject · 选中 Reject 可附一句"}
      </text>
    </box>
  )
}
