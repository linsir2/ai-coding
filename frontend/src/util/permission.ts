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
