/**
 * Session sidebar (M4) — the right column, a reduced OpenCode sidebar:
 * the session title, a Context card (real token usage), and the working
 * directory footer.  The MCP section is deferred (no MCP servers configured;
 * docs/opencode-gui-m4-plan.md §1).
 */

import { TextAttributes } from "@opentui/core"
import { Show } from "solid-js"
import { type JobRow } from "../state/session"
import { theme } from "../theme/tokens"
import { abbreviateHome, formatLocation, truncatePath } from "../util/location"
import { formatTokens, percentUsed } from "../util/stats"

/**
 * The one line the sidebar spends on jobs.  Running first: a finished job is
 * history the panel keeps, not something the operator has to act on.
 */
export function jobLine(jobs: readonly JobRow[]): string {
  const running = jobs.filter((job) => job.status === "running").length
  const others = jobs.length - running
  const parts = [`${running} 个后台任务运行中`]
  if (others > 0) parts.push(`${others} 个已结束`)
  return parts.join(" · ")
}

export const JOB_HINT = "<leader>j 查看 / 回车停止"

export function Sidebar(props: {
  title: string
  tokens: number
  maxContextTokens: number
  workspace: string
  branch: string
  /**
   * Background jobs (D-1b).  One line, and only when there is something to
   * say: a sidebar that always reserves the row would teach the eye to stop
   * reading it, which is the opposite of what a running dev server needs.
   */
  jobs?: JobRow[]
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
      <Show when={(props.jobs ?? []).length > 0}>
        <text fg={theme.textBase}>{jobLine(props.jobs ?? [])}</text>
        <text fg={theme.textMuted}>{JOB_HINT}</text>
      </Show>
      <text wrapMode="none" fg={theme.textMuted}>
        {location()}
      </text>
    </box>
  )
}
