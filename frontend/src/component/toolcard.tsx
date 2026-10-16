/**
 * Tool card (M2, folded in A5) — what a settled call reads like:
 *
 *   row     : ± ✓/✗/⊘  已运行命令 · git branch · 820ms      (click opens)
 *   folded  : the failure as a sentence, or the output preview
 *   open    : the whole tail, with a note when its head was dropped
 *
 * Three things the old card got wrong, all fixed here rather than in the
 * reducer: the failure was the raw machine code (`nonzero_exit`), the duration
 * the wire measured was thrown away, and 4,000 characters of output had no
 * way out.  The code → label table is `toolFailure()` in util/toolmeta (A5 §4).
 */

import { TextAttributes } from "@opentui/core"
import { For, Show, createSignal } from "solid-js"
import type { Part } from "../state/session"
import type { TailLine } from "../util/toolmeta"
import { theme } from "../theme/tokens"
import {
  formatDuration,
  toolFailure,
  toolTitle,
  truncationNote,
} from "../util/toolmeta"

type ToolPart = Extract<Part, { kind: "tool" }>

export function ToolCard(props: { part: ToolPart }) {
  // 20 §批次2: a settled todo_write is the plan card's event, not a card of
  // its own -- the raw `<task-state>` preview it used to print was half the
  // wall the user saw.  A failure still draws: the card is the only place a
  // refusal reaches the operator.
  if (props.part.toolName === "todo_write" && props.part.status !== "failed") return null

  // Per-card fold, like the reasoning block (A4 §3): view state, never state.
  const [expanded, setExpanded] = createSignal(false)
  const toggle = () => setExpanded((value) => !value)

  const running = () => props.part.status === "running"
  const failure = () => toolFailure(props.part.error)
  const denied = () => props.part.status !== "running" && failure().tone === "denied"

  const accent = () => {
    if (running()) return theme.textBase
    if (denied()) return theme.feedbackWarning
    return props.part.status === "ok" ? theme.feedbackSuccess : theme.feedbackError
  }

  const icon = () => (running() ? null : denied() ? "⊘" : props.part.status === "ok" ? "✓" : "✗")
  /** Only a settled call with output has something to open; no target, no lie. */
  const canExpand = () => !running() && props.part.lines.length > 0
  const strike = () => (denied() ? TextAttributes.STRIKETHROUGH : undefined)
  const tailLines = () => props.part.lines.slice(-3)

  return (
    <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
      {/* The whole row is the target (OpenCode message-parts.tsx:218): no
          `+`/`−` glyph — next to ✓/✗/⊘ it read as part of the outcome — and a
          row-wide target beats making the reader find the label. */}
      <box flexDirection="row" gap={1} onMouseUp={canExpand() ? toggle : undefined}>
        <Show when={!running()} fallback={<spinner name="dots" color={theme.textMuted} />}>
          <text fg={accent()} attributes={strike()}>
            {icon()}
          </text>
        </Show>
        <text fg={accent()} attributes={strike()}>
          {toolTitle(props.part.verb, props.part.target, running())}
        </text>
        <Show when={props.part.durationMs > 0}>
          <text fg={theme.textMuted}>{`· ${formatDuration(props.part.durationMs)}`}</text>
        </Show>
      </box>
      <Show when={!running() && failure().label} fallback={<Show when={running()}>
        <For each={tailLines()}>
          {(line) => (
            <text fg={line.stream === "stderr" ? theme.feedbackError : theme.textMuted}>
              {`│ ${line.text}`}
            </text>
          )}
        </For>
      </Show>}>
        <text fg={denied() ? theme.feedbackWarning : theme.feedbackError}>{failure().label}</text>
      </Show>
      <Show when={!running() && !failure().label && props.part.preview}>
        <text fg={theme.textMuted}>{props.part.preview}</text>
      </Show>
      <Show when={expanded() && canExpand()}>
        <box border={["left"]} borderColor={theme.backgroundRaisedMax} paddingLeft={1} flexShrink={0}>
          <box flexDirection="column">
            <Show when={props.part.linesDropped}>
              <text fg={theme.textMuted}>{truncationNote(true)}</text>
            </Show>
            <For each={props.part.lines}>
              {(line) => (
                <text fg={line.stream === "stderr" ? theme.feedbackError : theme.textMuted}>
                  {line.text}
                </text>
              )}
            </For>
          </box>
        </box>
      </Show>
    </box>
  )
}