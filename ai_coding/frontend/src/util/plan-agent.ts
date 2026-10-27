/**
 * Which agent id means "plan" (T7).
 *
 * One definition on the frontend, mirroring the backend's
 * ``domain/agents.py::is_plan_agent``: only the built-in plan agent carries
 * plan semantics.  A custom primary named anything else renders as itself.
 */

/** The built-in planning agent's id (backend PLAN_AGENT_ID). */
export const PLAN_AGENT_ID = "plan"

/** Whether this agent id is the plan agent. */
export function isPlanAgentId(agentId: string | null | undefined): boolean {
  return agentId === PLAN_AGENT_ID
}

/**
 * The posture suffix for an agent line (composer meta + status bar share it).
 *
 * The plan agent's display name is already "Plan", so repeating "PLAN" next
 * to it reads as a stutter ("Plan·PLAN·只读").  The name carries the plan
 * part; the suffix only keeps what the name does not say ("只读").  Any
 * other agent in a plan/readonly posture keeps the full suffix, so the
 * posture stays distinguishable even when the name does not imply it.
 */
export function postureSuffix(
  agentId: string | null | undefined,
  mode: "normal" | "plan" | undefined,
  readonly: boolean | undefined,
): string | null {
  const plan = mode === "plan"
  const ro = readonly === true
  const selfNamed = isPlanAgentId(agentId)
  if (plan && ro) return selfNamed ? "只读" : "PLAN·只读"
  if (plan) return selfNamed ? null : "PLAN"
  if (ro) return "只读"
  return null
}
