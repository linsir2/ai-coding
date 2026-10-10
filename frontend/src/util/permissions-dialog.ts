/**
 * Permissions dialog, the pure half (A3.1).
 *
 * The dialog reads through ``permissions.state`` and switches by dispatching
 * the backend's own commands — one mutation path, so the provider re-arm
 * (I1) stays where it is.  There is no policy beyond the two the callback
 * types express (J1).
 */

export type ApprovalPolicy = "auto" | "confirm"

export interface PermissionsState {
  policy: ApprovalPolicy
  grants: string[]
  storePath: string
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

export interface PermissionAction {
  key: "auto" | "confirm" | "clear"
  label: string
  /** The exact line the backend's dispatcher understands. */
  command: string
  description: string
}

export const PERMISSION_ACTIONS: PermissionAction[] = [
  {
    key: "auto",
    label: "Auto-approve",
    command: "/permissions auto",
    description: "Never ask; every call proceeds",
  },
  {
    key: "confirm",
    label: "Confirm each call",
    command: "/permissions confirm",
    description: "Ask before every warning-tier call",
  },
  {
    key: "clear",
    label: "Forget grants",
    command: "/permissions clear",
    description: "Drop remembered always-allow entries, on disk too",
  },
]

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
