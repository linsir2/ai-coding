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

export interface PermissionsState {
  policy: ApprovalPolicy
  grants: string[]
  storePath: string
  rules?: PermissionsRules
}

/** What reset sends: the session layer's own command, not a second store. */
export const RESET_SESSION_COMMAND = "/permissions reset"

/** One rule as the line the operator reads: effect, action, resource. */
export function ruleLine(rule: PermissionRule): string {
  const reason = rule.reason ? ` · ${rule.reason}` : ""
  return `${rule.effect} ${rule.action} ${rule.resource}${reason}`
}

export function policyLabel(policy: ApprovalPolicy): string {
  return policy === "auto" ? "Auto-approve" : "Confirm each call"
}

/** One grant as a readable line: the key is ``tool:path`` or just ``tool``. */
export function grantLine(grant: string): string {
  if (!grant) return "(none)"
  const separator = grant.indexOf(":")
  return separator < 0 ? grant : `${grant.slice(0, separator)} · ${grant.slice(separator + 1)}`
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
