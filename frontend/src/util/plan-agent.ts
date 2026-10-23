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
