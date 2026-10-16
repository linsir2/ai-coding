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
  /**
   * The keymap command a selection runs, or ``undefined`` for a **text**
   * command: the selection submits ``/<name>`` to ``session.command`` and the
   * backend's dispatcher decides what it means (A2.3).  An entry must have one
   * or the other — an entry pointing at a command that only the session route
   * registers silently did nothing on Home.
   */
  command?: string
  title: string
  description: string
}

/**
 * The line a catalogue selection submits.  A keymap entry returns nothing (the
 * command runs instead); a text entry returns its own slash line.
 */
export function slashSelectionText(entry: SlashCommand): string | undefined {
  return entry.command ? undefined : `/${entry.name}`
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
    name: "mcp",
    command: "mcp.list",
    title: "MCP servers",
    description: "Connected MCP servers, and connecting one",
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
  {
    name: "plan",
    command: "session.plan",
    title: "Plan",
    description: "Enter read-only plan mode (add a topic to plan it)",
  },
  {
    name: "review",
    command: "session.review",
    title: "Review",
    description: "Review the current git diff",
  },
  // ---- text commands: submitted verbatim, dispatched by the backend (A2.3).
  // Each one is a query or a requested action the backend already implements
  // (app/commands.py); the output lands in the transcript as notice lines.
  { name: "budget", title: "Budget", description: "This session's token ledger" },
  { name: "trace", title: "Trace", description: "Last turn's trace summary" },
  { name: "todo", title: "Todo", description: "Render the current task list" },
  {
    // M7: was a text command, so it printed the store's counts — 43 notes, 33
    // scene labels — and nothing you could read, pin or remove. It now opens the
    // panel. The backend keeps handling `/memory` for the non-TUI entry points.
    name: "memory",
    command: "dialog.memory.show",
    title: "Memory",
    description: "What the agent remembers, with pin and delete",
  },
  { name: "config", title: "Config", description: "Model and runtime limits in effect" },
  { name: "tasks", title: "Tasks", description: "Background sub-agent runs (add: stop <id>)" },
  { name: "compact", title: "Compact", description: "Compact this session's history now" },
  { name: "undo", title: "Undo", description: "Roll the last turn back (history + files)" },
  { name: "redo", title: "Redo", description: "Put back the turn /undo removed" },
  {
    // P5 (18 §10.1): the enforcement axis on its own; the plan entry composes it.
    name: "readonly",
    title: "Read-only",
    description: "Toggle a read-only session (writes refused; add: on|off)",
  },
  {
    name: "permissions",
    command: "session.permissions",
    title: "Permissions",
    description: "Show or switch the approval policy (add: auto|confirm|clear)",
  },
  { name: "stats", title: "Stats", description: "Every traced session, rolled up" },
  {
    name: "export",
    title: "Export",
    description: "Write traces out (add: html|otlp, optionally a session id)",
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
