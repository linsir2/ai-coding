/**
 * Permission panel, the pure half (M5).
 *
 * The panel answers a server-initiated ``approval.request`` (handlers.py::
 * _wire_approval); the answer must be a word the backend's shared parser
 * already understands, so "once" maps to ``yes``, not to its own token.
 */

export type PermissionDecision = "yes" | "always" | "no" | "stop"

export interface PermissionOption {
  key: "once" | "always" | "reject" | "stop"
  label: string
  decision: PermissionDecision
}

/** OpenCode's labels and order (util/permission.ts:162). */
export const PERMISSION_OPTIONS: PermissionOption[] = [
  { key: "once", label: "Allow once", decision: "yes" },
  { key: "always", label: "Always allow", decision: "always" },
  { key: "reject", label: "Reject", decision: "no" },
  // P0-4B: the fourth answer the backend has always understood -- the CLI has
  // advertised `s` since approval.py's prompt was written -- and the TUI could
  // not give. It refuses this call *and* ends the turn here, which is not what
  // esc does (esc cancels the run and leaves the turn unfinished), so it is a
  // distinct answer rather than a variant of "no".
  { key: "stop", label: "Stop this turn", decision: "stop" },
]

/**
 * The options this call can actually honour (P0-4A).
 *
 * "Always allow" writes down whatever `grant_key` names, and that returns
 * `None` for bash (a policy exclusion) and for any call whose arguments name no
 * single resource. Offering it anyway is a promise the gate keeps breaking: the
 * call runs once and the next identical call asks again, with the same button.
 *
 * The answer arrives with the request (`grantable`), so this rule stays in one
 * place -- the gate's. A request without the field predates the payload and gets
 * the full table, which is the honest default for a call that can be named.
 */
export function permissionOptions(request: { grantable?: boolean }): PermissionOption[] {
  if (request.grantable !== false) return PERMISSION_OPTIONS
  return PERMISSION_OPTIONS.filter((option) => option.decision !== "always")
}

/** How a refusal carries the operator's own words ("no::改用 ls -la"). */
export const NOTE_SEPARATOR = "::"

/**
 * The answer, with the operator's note when they wrote one (C-3).
 *
 * The note rides on the answer rather than in a second request: the backend
 * lifts it off before the shared parser sees the string (that parser serves
 * two front ends and speaks only the CLI vocabulary), and puts it on the user
 * lane, so it lands in the same request as the refusal it answers. A
 * whitespace-only note is dropped -- "no::" would make the operator look like
 * they had said something when they had not.
 */
export function permissionAnswer(decision: PermissionDecision, note = ""): string {
  const text = note.trim()
  return text ? `${decision}${NOTE_SEPARATOR}${text}` : decision
}

/** Why the remembered option is missing, in the panel's own voice. */
export const NO_GRANT_NOTE =
  "Not remembered for this call — it names no single resource (or policy excludes it), so it is asked every time."

/** What the panel is asked about: the wire payload, in panel terms. */
export interface PermissionRequest {
  toolName: string
  params: Record<string, unknown>
  reason: string
  /** Whether an "always" would be remembered (handlers.py sends the gate's own
   *  `grant_key` answer).  Absent means predates the payload: offer everything. */
  grantable?: boolean
}

/**
 * `approval.request` params -> the panel's request (P0-4A).
 *
 * The mapper is the whole reason `grantable` works: a field the panel needs but
 * nobody copies is a field the panel never sees, and the option silently comes
 * back. Keeping it here means the wiring is testable without a socket.
 */
export function permissionRequest(params: Record<string, unknown>): PermissionRequest {
  return {
    toolName: String(params["toolName"] ?? ""),
    params: (params["params"] as Record<string, unknown> | undefined) ?? {},
    reason: String(params["reason"] ?? ""),
    // `undefined` must survive as undefined: an older backend sends nothing, and
    // that means "offer everything", not "nothing can be remembered".
    grantable: typeof params["grantable"] === "boolean" ? params["grantable"] : undefined,
  }
}

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
