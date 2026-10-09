/**
 * Tool card (M2) — the three states OpenCode's BlockTool shows, with this
 * project's real verbs (ported from the old transcript's table):
 *
 *   running : <spinner> 正在运行命令 · ls      (+ live tail, last 3 lines)
 *   ok      : ✓ 已运行命令 · ls                (+ 120-char output preview)
 *   failed  : ✗ 已运行命令 · ls                (+ error code line)
 *
 * `tail` is capped by the reducer (4000 chars) — rendering only ever reads
 * the last three non-empty lines, so a `yes`-style firehose stays cheap.
 */

import { For } from "solid-js"
import type { Part } from "../state/session"
import { theme } from "../theme/tokens"
import { toolTitle } from "../util/toolmeta"

type ToolPart = Extract<Part, { kind: "tool" }>

export function ToolCard(props: { part: ToolPart }) {
  const running = () => props.part.status === "running"
  const color = () =>
    running()
      ? theme.textBase
      : props.part.status === "ok"
        ? theme.feedbackSuccess
        : theme.feedbackError

  const tailLines = () =>
    props.part.tail
      .split("\n")
      .filter((line) => line.length > 0)
      .slice(-3)

  return (
    <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
      <box flexDirection="row" gap={1}>
        {running() ? (
          <spinner name="dots" color={theme.textMuted} />
        ) : (
          <text fg={color()}>{props.part.status === "ok" ? "✓" : "✗"}</text>
        )}
        <text fg={color()}>{toolTitle(props.part.verb, props.part.target, running())}</text>
      </box>
      {running() ? (
        <For each={tailLines()}>{(line) => <text fg={theme.textMuted}>{`│ ${line}`}</text>}</For>
      ) : props.part.error ? (
        <text fg={theme.feedbackError}>{props.part.error}</text>
      ) : props.part.preview ? (
        <text fg={theme.textMuted}>{props.part.preview}</text>
      ) : null}
    </box>
  )
}
