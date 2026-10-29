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

/**
 * One row per command name.
 *
 * The keymap's registered view lists every layer's entries, and a session route
 * registers a twin of an app command under the same name (Review, Queued
 * input), so both copies reached the palette as two identical rows.  Keep the
 * last registration -- the route layer registers after the app layer and its
 * `run` is the one bound to the live session -- while preserving the position
 * of the first occurrence so the palette order stays stable.
 */
export function dedupeRowsByName(rows: readonly CommandRow[]): CommandRow[] {
  const byName = new Map<string, CommandRow>()
  for (const row of rows) byName.set(row.name, row)
  const seen = new Set<string>()
  const unique: CommandRow[] = []
  for (const row of rows) {
    if (seen.has(row.name)) continue
    seen.add(row.name)
    unique.push(byName.get(row.name)!)
  }
  return unique
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
    const rows = entries.map((entry) => {
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
    return dedupeRowsByName(rows)
  })
}
