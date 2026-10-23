/**
 * The one agent-color mapping (T7).
 *
 * No per-agent custom colors in M4: the plan agent reads the warning tone,
 * everything else the shared agent color.  Message tags, the composer border
 * and the status bar all read this, so one id is one color everywhere.
 */

import { theme } from "../theme/tokens"
import { isPlanAgentId } from "./plan-agent"

/** The accent for an agent id: plan reads warning, the rest agentColor. */
export function agentAccent(agentId: string): string {
  return isPlanAgentId(agentId) ? theme.feedbackWarning : theme.agentColor
}
