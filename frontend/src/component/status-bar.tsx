/**
 * Status bar (M4) — the bottom line: the working directory on the left, the
 * real context usage, the palette hint, and the app name/version on the right
 * (OpenCode's session footer).  No cost segment: this app has no pricing data.
 */

import { useTerminalDimensions } from "@opentui/solid"
import { theme } from "../theme/tokens"
import { abbreviateHome, formatLocation, truncatePath } from "../util/location"
import { formatTokens, percentUsed } from "../util/stats"

/** Fixed segments (usage, hint, name/version, gaps, padding) — the rest is path. */
const FIXED_WIDTH = 56

export function StatusBar(props: {
  workspace: string
  branch: string
  tokens: number
  maxContextTokens: number
  version: string
  interruptArmed?: boolean
}) {
  const dimensions = useTerminalDimensions()
  const locationColor = props.interruptArmed ? theme.feedbackWarning : theme.textMuted
  const location = () => {
    if (props.interruptArmed) return "Press esc again to interrupt"
    const label = formatLocation(abbreviateHome(props.workspace, process.env.HOME ?? ""), props.branch)
    return truncatePath(label, Math.max(8, dimensions().width - FIXED_WIDTH))
  }

  return (
    <box flexDirection="row" flexShrink={0} gap={2} paddingLeft={2} paddingRight={2}>
      <text wrapMode="none" flexGrow={1} fg={locationColor}>
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
