/**
 * MCP dialog, the pure half (MCP-MCP §2.3).
 *
 * The manager reports what it did; this turns that into the words and colours
 * the panel reads in.  Four states, three of them OpenCode's (`Connected ✓`,
 * `Failed !`, and `off` standing in for its `Disabled ○` — ours is a global
 * switch, not a per-server one) plus `filtered`, which is ours alone: the
 * R7d whitelist deliberately dropped tools, and showing that as "connected"
 * would hide the saving.
 */

export type McpStatus = "connected" | "failed" | "filtered" | "off"

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
    case "off":
      return "muted"
  }
}

/** `3 tools`, or `1 / 3 tools` when a whitelist was in play. */
export function mcpToolNote(server: McpServer): string {
  if (server.toolCount <= 0) return ""
  return server.wantedCount > 0
    ? `${server.toolCount} / ${server.wantedCount} tools`
    : `${server.toolCount} tools`
}
