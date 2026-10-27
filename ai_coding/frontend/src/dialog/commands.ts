/**
 * Command registry view (M3) — one reactive projection of the keymap's
 * reachable commands, shared by the palette and the help dialog so neither
 * re-derives shortcuts (the library's own `useKeymapSelector` re-runs the
 * selector on every keymap state change).
 */

import { formatCommandBindings } from "@opentui/keymap/extras"
import { useKeymap, useKeymapSelector } from "@opentui/keymap/solid"
import type { Accessor } from "solid-js"
import { LEADER_KEY } from "../keymap-addons"

export interface CommandRow {
  name: string
  title: string
  description?: string
  group: string
  /** True when the command opts into the palette (<leader>p, M5b). */
  palette: boolean
  /** The formatted keybinding hint, or "" when unbound. */
  shortcut: string
}

export function useCommandRows(): Accessor<CommandRow[]> {
  const keymap = useKeymap()
  return useKeymapSelector((km) => {
    const entries = km.getCommandEntries({ visibility: "registered" })
    const names = entries.map((entry) => entry.command.name)
    const bindings = km.getCommandBindings({ visibility: "registered", commands: names })
    // `<leader>` is a token; the human-facing form is the trigger key
    // (OpenCode passes the same tokenDisplay in context/keymap.tsx:455-474).
    const formatOptions = { tokenDisplay: { leader: LEADER_KEY } }
    return entries.map((entry) => {
      const command = entry.command
      return {
        name: command.name,
        title: String(command.title ?? command.name),
        description: command.desc ? String(command.desc) : undefined,
        group: String(command.group ?? ""),
        palette: Boolean(command.palette),
        shortcut: formatCommandBindings(bindings.get(command.name), formatOptions) ?? "",
      }
    })
  })
}
