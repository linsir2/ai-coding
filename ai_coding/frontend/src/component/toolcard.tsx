/**
 * Tool card (M2, folded in A5) — what a settled call reads like:
 *
 *   row     : ✓ 已运行命令 / ✗ 已尝试运行命令 / ⊘ 已尝试运行命令 · git branch · 820ms
 *                                                                  (click opens)
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
import { planText, submittedPlan } from "../util/ask"
import {
  formatToolDuration,
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

  // 23: same shape, one turn later.  A settled finish_research's conclusion is
  // drawn as prose (the research.concluded event, and the persisted tool
  // result on reload), so the card would only repeat the first 120 chars of
  // it under a "ran a tool" heading -- which is exactly the "filed under a
  // category instead of shown as an answer" the user reported.  A failure still
  // draws: a refusal (already_concluded / implementation_request / ...) is
  // the only thing that reaches the operator there.
  if (props.part.toolName === "finish_research" && props.part.status !== "failed") return null

  // Per-card fold, like the reasoning block (A4 §3): view state, never state.
  const [expanded, setExpanded] = createSignal(false)
  const toggle = () => setExpanded((value) => !value)

  const running = () => props.part.status === "running"
  const failure = () => toolFailure(props.part.error)
  const denied = () => props.part.status !== "running" && failure().tone === "denied"
  // M3 T11: the plan the call submitted, read from its own arguments.  `task`
  // is the execution state and changes under the run; this is the text the
  // human actually approved.
  const plan = () => submittedPlan(props.part.params ?? {})

  const accent = () => {
    if (running()) return theme.textBase
    if (denied()) return theme.feedbackWarning
    return props.part.status === "ok" ? theme.feedbackSuccess : theme.feedbackError
  }

  const icon = () => (running() ? null : denied() ? "⊘" : props.part.status === "ok" ? "✓" : "✗")
  /** Only a settled call with output has something to open; no target, no lie. */
  const canExpand = () =>
    !running() && (props.part.lines.length > 0 || plan() !== null)
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
          {toolTitle(props.part.verb, props.part.target, running(), props.part.status === "ok")}
        </text>
        <Show when={props.part.durationMs > 0}>
          {/* A-10: never let the wait note wrap or squeeze the tool name. */}
          <text flexShrink={0} wrapMode="none" fg={theme.textMuted}>
            {`· ${formatToolDuration(props.part.durationMs, props.part.approvalMs)}`}
          </text>
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
            <Show when={plan()}>
              {(artifact) => (
                <>
                  <text fg={theme.textBase}>{planText(artifact().plan)}</text>
                  <For each={artifact().items}>
                    {(item, index) => (
                      <box flexDirection="column" marginTop={1}>
                        <text fg={theme.textBase}>{`${index() + 1}. ${item.content}`}</text>
                        <text fg={theme.textMuted}>
                          {`   verify：${item.verify || "无证明命令"}` +
                            (item.dependsOn.length > 0
                              ? ` · 依赖 ${item.dependsOn.map((n) => `#${n}`).join(", ")}`
                              : "")}
                        </text>
                      </box>
                    )}
                  </For>
                </>
              )}
            </Show>
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
