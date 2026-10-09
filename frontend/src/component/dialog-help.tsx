/**
 * Help dialog (M3) — a read-only keybinding reference built from the same
 * command registry the palette uses (so it can never drift from reality).
 */

import { TextAttributes } from "@opentui/core"
import { useTerminalDimensions } from "@opentui/solid"
import { For, createMemo } from "solid-js"
import { useCommandRows } from "../dialog/commands"
import { theme } from "../theme/tokens"
import { groupOptions } from "../util/dialog-select"

export function DialogHelp() {
  const rows = useCommandRows()
  const dimensions = useTerminalDimensions()
  const groups = createMemo(() =>
    groupOptions(
      rows()
        .filter((row) => row.palette)
        .map((row) => ({
          title: row.title,
          value: row.name,
          category: row.group || "Other",
          footer: row.shortcut,
        })),
    ),
  )

  return (
    <box flexDirection="column" width="100%" paddingLeft={4} paddingRight={4} paddingBottom={1} gap={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
          Help
        </text>
        <text fg={theme.textMuted}>esc</text>
      </box>
      <scrollbox flexShrink={1} maxHeight={Math.max(3, Math.floor(dimensions().height / 2) - 6)}>
        <For each={groups()}>
          {(group) => (
            <box flexDirection="column" paddingTop={1}>
              <text fg={theme.hueAccent} attributes={TextAttributes.BOLD}>
                {group.category}
              </text>
              <For each={group.options}>
                {(option) => (
                  <box flexDirection="row" justifyContent="space-between" paddingLeft={2}>
                    <text wrapMode="none" fg={theme.textMuted}>{option.title}</text>
                    <text wrapMode="none" fg={theme.textMuted}>{option.footer}</text>
                  </box>
                )}
              </For>
            </box>
          )}
        </For>
      </scrollbox>
      <text fg={theme.textMuted}>esc close</text>
    </box>
  )
}
