/**
 * Slash-command autocomplete, the pure half (M5).
 *
 * Only commands this TUI can actually run are listed — the backend's REPL
 * vocabulary has entries with no TUI action, and offering a dead command would
 * send its text to the model as a plain prompt.  Each entry therefore points at
 * a keymap command or submits its own slash line.
 *
 * R20b: the two directions are now reconciled at submit time.  A typed line that
 * names an entry **with** a keymap command runs that command (see
 * ``prompt.tsx::submit``), so ``/models`` and ``/sessions`` — names the backend
 * has never known — no longer reach the model as prose, and a command that
 * exists on both sides (``/help``, ``/status``, ``/memory``) has one behaviour
 * instead of two.
 */

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
    // H3: the guided provider/model wizard.  Deliberately no "model" entry —
    // it ties with "models" for the query "mod", and `/model x` is already a
    // text command the backend dispatches (§4.4 #11).
    name: "connect",
    command: "dialog.connect.show",
    title: "Connect",
    description: "Add a provider or model",
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
    // R20b: pointed at "session.permissions", which no keymap ever registered —
    // selecting it cleared the composer and did nothing.  The dialog is the
    // TUI's own implementation (app.tsx registers it at the app layer because
    // the policy is workspace-global); typing "/permissions auto" still reaches
    // the backend's text handler, which is the only form that takes an argument.
    name: "permissions",
    command: "dialog.permissions.show",
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
 * The catalogue entry a typed line runs directly, or ``undefined``.
 *
 * Only an exact ``/name`` with no arguments: a keymap command takes none, and a
 * line with arguments (``/permissions auto``) belongs to the backend's text
 * dispatcher.  R20b — this is what stops ``/models``, ``/sessions``, ``/new``
 * and ``/mcp`` (names the backend has never known) from being submitted as
 * ordinary prose for the model to answer.
 */
export function ownCommand(text: string): SlashCommand | undefined {
  const line = text.trim()
  return SLASH_COMMANDS.find(
    (entry) => entry.command !== undefined && `/${entry.name}` === line,
  )
}

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

/**
 * Substring match on the **name only**, prefix first (R22/A-2).
 *
 * This used to be ``go()`` over ``["name","description"]`` with ``threshold: 0``.
 * Two things went wrong with that.  Descriptions are prose, so ``/clear`` hit
 * ``/permissions`` ("auto|confirm|clear") and ``/agents`` hit ``/tasks``
 * ("sub-agent runs"); and fuzzysort matches *subsequences*, so ``/readonly``
 * matched ``/redo``.  In the TUI the selection is what Enter runs
 * (``prompt.tsx::prompt.autocomplete.select``), so a typed backend command the
 * catalogue does not list was silently replaced by a neighbour — measured:
 * ``/readonly`` ran ``/export``, ``/plan`` opened the permissions dialog,
 * ``/agents`` ran ``/tasks``, ``/clear`` opened permissions, ``/resume`` ran
 * ``/export``.
 *
 * A substring of the name cannot do that: no name contains "readonly", so the
 * list is empty, the popup closes, and Enter submits the line to the backend
 * dispatcher that has always known it.  Descriptions stay visible on the row;
 * the command palette (ctrl+x p) still searches them.
 */
export function filterSlashCommands(query: string): SlashCommand[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return SLASH_COMMANDS
  const hits = SLASH_COMMANDS.map((entry) => ({
    entry,
    at: entry.name.toLowerCase().indexOf(needle),
  })).filter((row) => row.at >= 0)
  // A prefix ("mod" → models) outranks a mid-word hit; shorter names first so
  // "re" lists redo before review rather than in catalogue order.
  hits.sort((a, b) => a.at - b.at || a.entry.name.length - b.entry.name.length)
  return hits.map((row) => row.entry)
}
