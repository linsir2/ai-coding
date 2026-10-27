/**
 * Command palette (M3, Ctrl+P) — OpenCode's command-palette.tsx: every
 * reachable command that opts into the palette, fuzzy-searchable, with its
 * keybinding as the row footer.  Selecting one closes the palette and runs it.
 */

import { useKeymap } from "@opentui/keymap/solid"
import { useDialog } from "../dialog/context"
import { useCommandRows } from "../dialog/commands"
import { DialogSelect } from "./dialog-select"

export function CommandPalette() {
  const dialog = useDialog()
  const keymap = useKeymap()
  const rows = useCommandRows()

  const options = () =>
    rows()
      .filter((row) => row.palette)
      .map((row) => ({
        title: row.title,
        value: row.name,
        description: row.description,
        category: row.group,
        footer: row.shortcut,
      }))

  return (
    <DialogSelect
      title="Commands"
      options={options()}
      threshold={0.7}
      onSelect={(option) => {
        dialog.close()
        keymap.runCommand(option.value)
      }}
      footer="enter run · esc close"
    />
  )
}
