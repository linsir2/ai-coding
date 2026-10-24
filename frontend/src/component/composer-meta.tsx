/**
 * The composer's metadata line (19 §3.1, T7) — the agent name first, then
 * the posture suffix, then the model/provider line.
 *
 * T7: a missing agent (an older backend payload) reads as Build, the same
 * fail-safe direction as the stats contract itself.  Extracted from
 * ``prompt.tsx`` so the frame is testable without the composer's
 * keymap/clipboard machinery (status-bar precedent).
 */

import { TextAttributes } from "@opentui/core"
import { theme } from "../theme/tokens"
import { agentAccent } from "../util/agent"
import { postureSuffix } from "../util/plan-agent"

export function ComposerMeta(props: {
  /** T7: the current agent id — the name leads the line. */
  agent?: string | null
  /** Display name when the caller resolved one (falls back to the id). */
  agentName?: string | null
  mode?: "normal" | "plan"
  readonly?: boolean
  metadata: string
}) {
  const name = () => props.agentName ?? props.agent ?? "Build"
  // The plan agent is already named "Plan" — postureSuffix folds the
  // redundant PLAN away, keeping only what the name does not say.
  const suffix = () => postureSuffix(props.agent, props.mode, props.readonly)
  return (
    <box flexDirection="row">
      <text fg={agentAccent(props.agent ?? "")} attributes={TextAttributes.BOLD}>
        {name()}
      </text>
      {suffix() ? (
        <>
          <text fg={theme.textMuted}> · </text>
          <text
            fg={props.mode === "plan" ? theme.feedbackWarning : theme.textMuted}
            attributes={props.mode === "plan" ? TextAttributes.BOLD : undefined}
          >
            {suffix()}
          </text>
        </>
      ) : null}
      <text fg={theme.textMuted}> · </text>
      <text fg={theme.textMuted}>{props.metadata}</text>
    </box>
  )
}
