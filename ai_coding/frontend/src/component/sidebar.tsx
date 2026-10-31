/**
 * Session sidebar (M4) — the right column, a reduced OpenCode sidebar: the
 * session title, a Context card (real token usage), and (M4b) a session card
 * carrying the facts the transcript never shows: turns, message rows, when
 * the session began, and the session budget.  The job block lists only what
 * is running; settled rows are the panel's business.
 *
 * What the sidebar does NOT show (2026-10-18, operator feedback): the working
 * directory and branch — the status bar already renders them — and anything
 * else the footer/composer already say.  One fact, one home.
 */

import { TextAttributes } from "@opentui/core"
import { For, Show } from "solid-js"
import type { BudgetFigures, GoalStatus } from "../state/session"
import { type JobRow } from "../state/session"
import { theme } from "../theme/tokens"
import { formatTokens, percentUsed } from "../util/stats"

/** M4b: turns and message rows, the session card's first line. */
export function sessionLine(turns: number, messages: number): string {
  return turns > 0 ? `${turns} 轮 · ${messages} 条消息` : `尚无回合 · ${messages} 条消息`
}

/** Local-clock HH:MM of an ISO stamp; ``""`` when unparseable. */
function clockOf(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  const hours = String(date.getHours()).padStart(2, "0")
  const minutes = String(date.getMinutes()).padStart(2, "0")
  return `${hours}:${minutes}`
}

/** ``13:45 起 · 已 26 分钟``; ``""`` when the stamp is unusable. */
export function startedLine(createdIso: string, now: Date): string {
  const clock = clockOf(createdIso)
  if (!clock) return ""
  const created = new Date(createdIso)
  const minutes = Math.max(0, Math.floor((now.getTime() - created.getTime()) / 60000))
  if (minutes < 1) return `${clock} 起 · 刚刚开始`
  if (minutes < 60) return `${clock} 起 · 已 ${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest > 0 ? `${clock} 起 · 已 ${hours} 小时 ${rest} 分` : `${clock} 起 · 已 ${hours} 小时`
}

/** ``累计 ¥0.42 · 缓存 76%``; ``null`` (the line hides) when no price is known.
 *
 * "累计" is not decoration: the figure sums every model step the recorder saw,
 * plan waves and sub-agents included, while the line above counts this
 * session's own turns.  Saying "累计" keeps the two from reading as a
 * contradiction (measured trace 2026-10-31: 4 turns beside ¥75.64 from 117
 * steps).  When no cached rate is configured the cost charges cache hits at
 * the input price, so the line says that too instead of presenting an upper
 * bound as the bill.
 */
export function budgetLine(budget: BudgetFigures | null | undefined): string | null {
  if (!budget || typeof budget.cost !== "number") return null
  const money = `累计 ¥${budget.cost.toFixed(2)}`
  const cache =
    typeof budget.cachedPercent === "number" ? ` · 缓存 ${budget.cachedPercent}%` : ""
  const unknown = budget.cachedPriceKnown === false ? "（缓存按输入价）" : ""
  return `${money}${cache}${unknown}`
}

export const JOBS_HEADING = "后台任务"
export const JOB_HINT = "<leader>j 查看 / ctrl+d 停止"
/** More running rows than this collapse into a count (the panel has them all). */
export const MAX_JOB_ROWS = 3
const JOB_COMMAND_CHARS = 16

/**
 * The job block's rows: running jobs only, one per line, with their start
 * clock (static — the block refreshes on job events, not on a timer).
 * Settled rows live in the panel; a permanent line counting them was the
 * furniture this replaced (2026-10-18).
 */
export function jobLines(jobs: readonly JobRow[]): string[] {
  const running = jobs.filter((job) => job.status === "running")
  if (running.length === 0) return []
  const lines = running.slice(0, MAX_JOB_ROWS).map((job) => {
    const command =
      job.command.length > JOB_COMMAND_CHARS
        ? `${job.command.slice(0, JOB_COMMAND_CHARS)}…`
        : job.command
    const started = clockOf(job.startedAt)
    return `${job.id.slice(0, 12)} ${command}${started ? ` · 起于 ${started}` : ""}`
  })
  if (running.length > MAX_JOB_ROWS) {
    lines.push(`…还有 ${running.length - MAX_JOB_ROWS} 个运行中`)
  }
  return lines
}

/**
 * G2: the one line a running goal gets.  Same rule as jobLines -- the line
 * only exists while it has something true to say (here: while armed), so it
 * never turns into furniture.
 */
export function goalLine(goal: GoalStatus): string {
  const round = goal.roundsRun > 0 ? `第 ${goal.roundsRun} 轮` : "进行中"
  const spent = goal.tokensUsed > 0 ? ` · ${formatTokens(goal.tokensUsed)} tokens` : ""
  return `目标：${round}${spent}`
}

export function Sidebar(props: {
  title: string
  tokens: number
  maxContextTokens: number
  /** M4b: the session card's facts (stats + the client's own message count). */
  turns: number
  messages: number
  createdAt: string
  budget?: BudgetFigures | null
  /** Background jobs (D-1b): running rows only, hidden when none run. */
  jobs?: JobRow[]
  /** G2: the goal episode's live status; the line shows only while armed. */
  goal?: GoalStatus | null
}) {
  const started = () => startedLine(props.createdAt, new Date())
  const money = () => budgetLine(props.budget)
  const jobRows = () => jobLines(props.jobs ?? [])

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
      <box flexDirection="column" paddingTop={1}>
        <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
          本次会话
        </text>
        <text fg={theme.textMuted}>{sessionLine(props.turns, props.messages)}</text>
        <Show when={started()}>
          <text fg={theme.textMuted}>{started()}</text>
        </Show>
        <Show when={money()}>
          <text fg={theme.textMuted}>{money()}</text>
        </Show>
        {/* The cost above can cover plan waves and sub-agents; this line names
          them only when the summary counted more steps than this session's
          own turns (the measured 4-turn/117-step case). */}
        <Show when={(props.budget?.steps ?? 0) > props.turns}>
          <text fg={theme.textMuted}>
            {`含计划步/子代理（累计 ${props.budget?.steps} 步）`}
          </text>
        </Show>
      </box>
      <Show when={props.goal?.armed ? props.goal : undefined}>
        {(goal) => <text fg={theme.textBase}>{goalLine(goal())}</text>}
      </Show>
      <Show when={jobRows().length > 0}>
        <box flexDirection="column" paddingTop={1}>
          <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
            {JOBS_HEADING}
          </text>
          <For each={jobRows()}>{(line) => <text fg={theme.textBase}>{line}</text>}</For>
          <text fg={theme.textMuted}>{JOB_HINT}</text>
        </box>
      </Show>
      <box flexGrow={1} />
    </box>
  )
}
