/**
 * Session sidebar (M4) — the right column, a reduced OpenCode sidebar:
 * the session title, a Context card (real token usage), and the working
 * directory footer.  The MCP section is deferred (no MCP servers configured;
 * docs/opencode-gui-m4-plan.md §1).
 */

import { TextAttributes } from "@opentui/core"
import { Show } from "solid-js"
import { theme } from "../theme/tokens"
import { abbreviateHome, formatLocation, truncatePath } from "../util/location"
import { formatTokens, percentUsed } from "../util/stats"

export function Sidebar(props: {
  title: string
  tokens: number
  maxContextTokens: number
  workspace: string
  branch: string
}) {
  const location = () => {
    const label = formatLocation(abbreviateHome(props.workspace, process.env.HOME ?? ""), props.branch)
    return truncatePath(label, 38)
  }

  return (
    <box
      width={42}
      flexShrink={0}
      height="100%"
      backgroundColor={theme.backgroundRaised}
      paddingTop={1}
      paddingBottom={1}
      paddingLeft={2}
      paddingRight={2}
      flexDirection="column"
    >
      <text wrapMode="none" fg={theme.textBase} attributes={TextAttributes.BOLD}>
        {props.title}
      </text>
      <box flexDirection="column" paddingTop={1}>
        <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
          Context
        </text>
        <text fg={theme.textMuted}>{`${formatTokens(props.tokens)} tokens`}</text>
        <Show when={props.maxContextTokens > 0}>
          <text fg={theme.textMuted}>{`${percentUsed(props.tokens, props.maxContextTokens)}% used`}</text>
        </Show>
      </box>
      <box flexGrow={1} />
      <text wrapMode="none" fg={theme.textMuted}>
        {location()}
      </text>
    </box>
  )
}
