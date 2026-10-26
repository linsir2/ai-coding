/**
 * Permissions dialog, the pure half (A3.1, moved to its own dialog in A4).
 *
 * The dialog reads through ``permissions.state`` and switches by dispatching
 * the backend's own commands — one mutation path, so the provider re-arm
 * (I1) stays where it is.  There is no policy beyond the two the callback
 * types express (J1).  A4 cut the PERMISSION_ACTIONS catalogue: it had no
 * consumer outside its own test, and the dialog is key-driven, not a menu.
 */

export type ApprovalPolicy = "auto" | "confirm"

export type RuleEffect = "allow" | "ask" | "deny"

export interface PermissionRule {
  action: string
  resource: string
  effect: RuleEffect
  reason?: string
  postures?: string[]
}

export interface PermissionsRules {
  layers: Record<string, number>
  session: PermissionRule[]
  errors: string[]
}

/** One standing grant as the panel shows it: the key and, when it has one, its deadline. */
export interface GrantRecord {
  key: string
  /** Epoch seconds; ``null`` means the grant is permanent (a v1 grant, or TTL disabled). */
  expiresAt: number | null
}

export interface PermissionsState {
  policy: ApprovalPolicy
  grants: GrantRecord[]
  /** M-D3: this session's one-off "yes" answers -- shown so they are not mistaken for standing grants. */
  onceGrants: string[]
  storePath: string
  rules?: PermissionsRules
}

/** What reset sends: the session layer's own command, not a second store. */
export const RESET_SESSION_COMMAND = "/permissions reset"

/** The dispatch line that forgets one grant (M-D3). */
export function revokeGrantCommand(key: string): string {
  return `/permissions revoke ${key}`
}

/** One rule as the line the operator reads: effect, action, resource. */
export function ruleLine(rule: PermissionRule): string {
  const reason = rule.reason ? ` · ${rule.reason}` : ""
  return `${rule.effect} ${rule.action} ${rule.resource}${reason}`
}

export function policyLabel(policy: ApprovalPolicy): string {
  return policy === "auto" ? "Auto-approve" : "Confirm each call"
}

/** One grant as a readable line: the key is ``tool:path`` or just ``tool``. */
export function grantLine(grant: GrantRecord): string {
  if (!grant || !grant.key) return "(none)"
  const separator = grant.key.indexOf(":")
  const base = separator < 0 ? grant.key : grant.key.slice(0, separator)
  const rest = separator < 0 ? "" : grant.key.slice(separator + 1)
  const expiry = grant.expiresAt
    ? ` · expires ${new Date(grant.expiresAt * 1000).toISOString()}`
    : ""
  return separator < 0 ? `${base}${expiry}` : `${base} · ${rest}${expiry}`
}

/**
 * The other policy — what a toggle moves to.  OpenCode's config dialog flips
 * between ``prompt`` and ``autoaccept``; ours are the same two states under
 * the dispatcher's own names (J1), so the toggle is a pair, not a menu.
 */
export function toggledPolicy(policy: ApprovalPolicy): ApprovalPolicy {
  return policy === "auto" ? "confirm" : "auto"
}

/** The dispatch line a policy toggle sends. */
export function toggleCommand(policy: ApprovalPolicy): string {
  return toggledPolicy(policy) === "auto" ? "/permissions auto" : "/permissions confirm"
}
