/**
 * MCP dialog, the pure half (MCP-MCP §2.3).
 *
 * The manager reports what it did; this turns that into the words and colours
 * the panel reads in.  Four settled states, three of them OpenCode's
 * (`Connected ✓`, `Failed !`, and `off` standing in for its `Disabled ○` —
 * ours is a global switch, not a per-server one) plus `filtered`, which is
 * ours alone: the R7d whitelist deliberately dropped tools, and showing that
 * as "connected" would hide the saving.  `connecting` is M3's: startup does
 * not wait for a server, so a first attempt in flight is a state the operator
 * can actually see (OpenCode's `pending` words it, `Connecting …`).
 */

export type McpStatus =
  | "connected"
  | "failed"
  | "filtered"
  | "off"
  | "connecting"
  // M4: a remote server that answered 401 — waiting on the operator, not on a
  // timer.  OpenCode words it "Sign in required →", and the arrow is literal:
  // the row's action starts a sign-in.
  | "needs_auth"

export interface McpServer {
  name: string
  /** The command and args as launched, so a row says which server it is. */
  command: string
  status: McpStatus
  /** Tools that actually reached the registry. */
  toolCount: number
  /** How many the whitelist asked for; 0 means no whitelist. */
  wantedCount: number
  /** Why it failed, or which tools the whitelist dropped. */
  detail: string
}

export type McpTone = "muted" | "success" | "warn" | "error"

/** OpenCode's wording for the two states it shares; ours for the other two. */
export function mcpStatusLabel(status: McpStatus): string {
  switch (status) {
    case "connected":
      return "Connected ✓"
    case "failed":
      return "Failed !"
    case "filtered":
      return "已过滤"
    case "connecting":
      return "Connecting …"
    case "needs_auth":
      return "Sign in required →"
    case "off":
      return "未启用"
  }
}

export function mcpStatusTone(status: McpStatus): McpTone {
  switch (status) {
    case "connected":
      return "success"
    case "failed":
      return "error"
    // Dropping tools was a choice the operator made to hold the per-step cost
    // down, so it reads as a note rather than a fault.
    case "filtered":
      return "warn"
    // Not settled either way yet: the neutral colour is the honest one.
    case "connecting":
      return "muted"
    case "needs_auth":
      return "warn"
    case "off":
      return "muted"
  }
}

/**
 * Parse the wizard's one-line command into argv (M5).
 *
 * Splits on whitespace, so `python -m srv --flag` becomes the argv the config
 * wants.  Quotes are honoured, because a path with a space is exactly the case
 * where a naive split would hand the child process a broken argument.
 */
export function parseCommandLine(line: string): string[] {
  const argv: string[] = []
  let current = ""
  let quote: '"' | "'" | null = null
  for (const char of line.trim()) {
    if (quote) {
      if (char === quote) quote = null
      else current += char
    } else if (char === '"' || char === "'") {
      quote = char
    } else if (/\s/.test(char)) {
      if (current) argv.push(current)
      current = ""
    } else {
      current += char
    }
  }
  if (current) argv.push(current)
  return argv
}

/**
 * Whether a row offers the panel's one action (M3).
 *
 * Exactly one state qualifies: `failed`.  A connected server has nothing to
 * retry, a disabled one cannot be connected at all, and a connecting one is
 * already trying — offering the action there would be a button that either
 * does nothing or duplicates what is already happening.
 */
export function mcpRowCanConnect(status: McpStatus): boolean {
  return status === "failed"
}

/**
 * Whether a row's action is a sign-in rather than a reconnect (M4).
 *
 * Exactly one state offers it, and it is not the same state as a reconnect: a
 * needs_auth server is waiting on a human, so the action opens a browser rather
 * than re-running a connect that will only 401 again.
 */
export function mcpRowCanLogin(status: McpStatus): boolean {
  return status === "needs_auth"
}

/** `3 tools`, or `1 / 3 tools` when a whitelist was in play. */
export function mcpToolNote(server: McpServer): string {
  if (server.toolCount <= 0) return ""
  return server.wantedCount > 0
    ? `${server.toolCount} / ${server.wantedCount} tools`
    : `${server.toolCount} tools`
}

/** The fields the wizard collects, per kind — the shape both halves agree on. */
export interface McpAddValues {
  name?: string
  command?: string
  url?: string
  token?: string
}

/**
 * The `mcp.add` request body (M7).
 *
 * A remote token is sent as `token`, never pre-baked into a header here: the
 * backend owns what becomes an `Authorization` header, so the secret exists in
 * exactly one place on the way through. A blank token is dropped rather than
 * sent as `""`, which a server reads as bad credentials rather than as none.
 */
export function mcpAddBody(
  kind: "local" | "remote",
  values: McpAddValues,
  trust: boolean,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: (values.name ?? "").trim(),
    type: kind,
    auto_approve: trust,
  }
  if (kind === "local") {
    body.command = parseCommandLine(values.command ?? "")
    return body
  }
  body.url = (values.url ?? "").trim()
  const token = (values.token ?? "").trim()
  if (token) body.token = token
  return body
}


export interface MemoryNote {
  id: string
  text: string
  /** One of fact / decision / preference / open_thread. */
  kind: string
  /** The extractor's free-text topic label; a subtitle and a filter, never a
   *  group — 33 of them for 43 notes, several near-duplicates. */
  scene: string
  // M10: "user" means the note lives outside any workspace and is recalled from
  // every project; anything else is this project's own.
  scope?: string
  pinned: boolean
  importance: number
  /** "tool" when a tool call in that turn backed it, "model" when the turn only
   *  talked. Provenance for a human reading the row. */
  evidence: string
  hits: number
  updated: string
}

/** The kind labels the panel groups by, in the order an operator reads them. */
export const MEMORY_KIND_LABELS: Record<string, string> = {
  decision: "决策",
  preference: "偏好",
  fact: "事实",
  open_thread: "未完事项",
}

/** One memory as the panel's row: the note itself, then what it is and where
 *  it came from. A pinned note says so in words — the panel has no per-row
 *  colour, and a fact only the model asserted is worth naming as such. */
export function memoryRowLine(note: MemoryNote): { title: string; description: string; category: string } {
  const kind = MEMORY_KIND_LABELS[note.kind] ?? note.kind
  // The pin is already marked on the title; repeating it here would be two
  // signals for one fact. What the subtitle adds is provenance.
  const marks = [note.evidence === "tool" ? "有工具佐证" : note.evidence === "model" ? "仅模型陈述" : ""]
    .filter(Boolean)
    .join(" · ")
  const title = `${note.pinned ? "📌 " : ""}${note.text}`
  const tier = note.scope === "user" ? "跨项目" : "本项目"
  const detail = [kind, marks, note.scene, tier].filter(Boolean).join(" · ")
  return { title, description: detail, category: kind }
}
