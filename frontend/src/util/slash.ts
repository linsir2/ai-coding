/**
 * Slash-command autocomplete, the pure half (M5).
 *
 * Only commands this TUI can actually run are listed — the backend's REPL
 * vocabulary (the retired ui/slash.py) has 21 entries, but 14 of them have no
 * TUI action, and offering a dead command would send its text to the model
 * as a plain prompt.  Each entry therefore points at a keymap command.
 */

import { go } from "fuzzysort"

export interface SlashCommand {
  /** Canonical name without the slash, e.g. ``help``. */
  name: string
  /** The keymap command a selection runs. */
  command: string
  title: string
  description: string
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    name: "help",
    command: "dialog.help.show",
    title: "Help",
    description: "Commands and keybindings",
  },
  {
    name: "models",
    command: "dialog.models.show",
    title: "Models",
    description: "Switch the active model",
  },
  {
    name: "sessions",
    command: "dialog.sessions.show",
    title: "Sessions",
    description: "Open a saved session",
  },
  {
    name: "skills",
    command: "dialog.skills.show",
    title: "Skills",
    description: "List available skills",
  },
  {
    name: "status",
    command: "dialog.status.show",
    title: "Status",
    description: "Session and runtime status",
  },
  {
    name: "new",
    command: "session.new",
    title: "New session",
    description: "Start a fresh session",
  },
  {
    name: "quit",
    command: "app.exit",
    title: "Quit",
    description: "Exit the app",
  },
]

/**
 * The command query before the cursor, or ``null`` when no token is active.
 *
 * A ``/`` opens a command only at a word boundary (start or after whitespace),
 * with no space between it and the cursor and no second slash — ``src/main.ts``
 * and ``/path/to`` stay plain text.  Returns the text after the ``/``; a bare
 * ``/`` yields ``""``.
 */
export function slashQuery(text: string, cursor: number): string | null {
  const upto = text.slice(0, Math.max(0, Math.min(cursor, text.length)))
  for (let index = upto.lastIndexOf("/"); index >= 0; index = upto.lastIndexOf("/", index - 1)) {
    const before = index === 0 ? undefined : upto[index - 1]
    const query = upto.slice(index)
    if (before !== undefined && !/\s/.test(before)) continue
    if (/\s/.test(query) || query.slice(1).includes("/")) return null
    return query.slice(1)
  }
  return null
}

/** Fuzzy-match the catalogue; an empty query keeps it (threshold 0, like OpenCode's command mode). */
export function filterSlashCommands(query: string): SlashCommand[] {
  const needle = query.trim()
  if (!needle) return SLASH_COMMANDS
  return go(needle, SLASH_COMMANDS, {
    keys: ["name", "description"],
    scoreFn: (result) => result[0]!.score * 2 + result[1]!.score,
    threshold: 0,
  }).map((result) => result.obj)
}
