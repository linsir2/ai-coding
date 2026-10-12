/**
 * Permission panel, the pure half (M5).
 *
 * The panel answers a server-initiated ``approval.request`` (handlers.py::
 * _wire_approval); the answer must be a word the backend's shared parser
 * already understands, so "once" maps to ``yes``, not to its own token.
 */

export type PermissionDecision = "yes" | "always" | "no"

export interface PermissionOption {
  key: "once" | "always" | "reject"
  label: string
  decision: PermissionDecision
}

/** OpenCode's labels and order (util/permission.ts:162). */
export const PERMISSION_OPTIONS: PermissionOption[] = [
  { key: "once", label: "Allow once", decision: "yes" },
  { key: "always", label: "Always allow", decision: "always" },
  { key: "reject", label: "Reject", decision: "no" },
]

/** How much of one argument the panel will print. */
const VALUE_LIMIT = 160
/** How many lines the whole block may take, so a big payload cannot scroll
 *  the answer keys off screen — the operator must still be able to answer. */
const BLOCK_LIMIT = 12

/**
 * What the approval panel prints about the call, for the tools whose own line
 * says nothing: an MCP call arrives as `server__tool` plus
 * `{server, tool, input}`, and `toolTarget` can only echo the name back. So the
 * operator was asked to approve `fs__write_file` without ever seeing which file
 * it would write. The arguments reached the frontend all along
 * (handlers.py's approval provider sends `params`); they were dropped at render
 * time. This is the half that prints them.
 *
 * Local tools are left alone on purpose: `toolTarget` already shows a read's
 * path and a bash command, and repeating the whole params object there is noise.
 */
export function permissionPayload(request: {
  toolName: string
  params: Record<string, unknown>
}): string[] {
  const { toolName, params } = request
  const isRemote = typeof params["server"] === "string" && "input" in params
  if (!isRemote) return []
  // A tool we already summarize does not need its params restated.
  if (!toolName.includes("__")) return []

  const input = params["input"]
  const lines: string[] = []

  if (input === null || input === undefined) return lines
  if (typeof input !== "object" || Array.isArray(input)) {
    lines.push(`input: ${clip(String(input))}`)
    return lines
  }

  for (const [key, value] of Object.entries(input as Record<string, unknown>)) {
    lines.push(`${key}: ${clip(render(value))}`)
    if (lines.length >= BLOCK_LIMIT) {
      const shown = Object.keys(input as Record<string, unknown>).length
      if (shown > lines.length - 1) lines.push(`… ${shown - (lines.length - 1)} more argument(s)`)
      break
    }
  }
  return lines.slice(0, BLOCK_LIMIT)
}

/** One argument, as one line: strings as themselves, the rest as JSON. */
function render(value: unknown): string {
  if (typeof value === "string") return value
  try {
    return JSON.stringify(value) ?? String(value)
  } catch {
    return String(value)
  }
}

/** Long values are cut rather than wrapped: this is a glance, not a diff. */
function clip(value: string): string {
  const flat = value.replace(/\r/g, "").replace(/\n/g, "⏎")
  return flat.length > VALUE_LIMIT ? `${flat.slice(0, VALUE_LIMIT - 1)}…` : flat
}
