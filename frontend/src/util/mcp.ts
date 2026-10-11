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
