/**
 * Status bar (M4, T7) — the bottom line: the agent first, the working
 * directory, the real context usage, the palette hint, and the app
 * name/version on the right (OpenCode's session footer).  No cost segment:
 * this app has no pricing data.
 */

import { Show } from "solid-js"
import { useTerminalDimensions } from "@opentui/solid"
import { theme } from "../theme/tokens"
import { abbreviateHome, formatLocation, truncatePath } from "../util/location"
import { formatTokens, percentUsed } from "../util/stats"
import { agentAccent } from "../util/agent"
import { postureSuffix } from "../util/plan-agent"

/** Fixed segments (usage, hint, name/version, gaps, padding) — the rest is path. */
const FIXED_WIDTH = 56
/** The longest agent-plus-posture label plus its gap — deducted while shown. */
const MARKER_WIDTH = 24

export function StatusBar(props: {
  workspace: string
  branch: string
  tokens: number
  maxContextTokens: number
  version: string
  interruptArmed?: boolean
  /** T7: the current agent id — its name leads the bar. */
  agent?: string | null
  /** Display name when the caller resolved one (falls back to the id). */
  agentName?: string | null
  /** P5 (18 §10.5): the prompt posture, "normal" | "plan". */
  mode?: "normal" | "plan"
  /** P5 (18 §10.5): the enforcement switch. */
  readonly?: boolean
}) {
  const dimensions = useTerminalDimensions()
  const locationColor = () => (props.interruptArmed ? theme.feedbackWarning : theme.textMuted)
  // T7: the agent name is the headline, PLAN·只读 a suffix (a missing agent
  // reads as Build, the stats contract's fail-safe direction).
  const marker = () => {
    const name = props.agentName ?? props.agent ?? "Build"
    // Same fold as the composer line: the plan agent's own name already
    // says Plan, so the suffix keeps only what the name does not say.
    const suffix = postureSuffix(props.agent, props.mode, props.readonly)
    return suffix ? `${name}·${suffix}` : name
  }
  const location = () => {
    if (props.interruptArmed) return "Press esc again to interrupt"
    const label = formatLocation(abbreviateHome(props.workspace, process.env.HOME ?? ""), props.branch)
    const reserve = FIXED_WIDTH + MARKER_WIDTH
    return truncatePath(label, Math.max(8, dimensions().width - reserve))
  }

  return (
    <box flexDirection="row" flexShrink={0} gap={2} paddingLeft={2} paddingRight={2}>
      <text wrapMode="none" fg={agentAccent(props.agent ?? "")}>
        {marker()}
      </text>
      <text wrapMode="none" flexGrow={1} fg={locationColor()}>
        {location()}
      </text>
      <text wrapMode="none" fg={theme.textMuted}>
        {`${formatTokens(props.tokens)} (${percentUsed(props.tokens, props.maxContextTokens)}%)`}
      </text>
      <text wrapMode="none" fg={theme.textMuted}>
        ctrl+x p commands
      </text>
      <text wrapMode="none" fg={theme.textMuted}>
        {`• ai-coding ${props.version}`}
      </text>
    </box>
  )
}
