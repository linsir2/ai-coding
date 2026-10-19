/**
 * Plan review card (19 §3.2, R04) — a presented plan, in the prompt's slot
 * while ``present_plan`` waits for the human (AskPanel's sibling: same slot,
 * same "the answer IS the RPC response" contract).  The card shows the plan
 * in full — a scrolling region, no more twelve-line excerpt — followed by
 * the item table: the rows approval will actually write, verify command and
 * dependencies included.  What the user reads is what the run executes.
 * Answers use the JSON codec from util/ask.ts; esc stays the shared "nobody
 * answered" (``reason="empty"`` → the backend's dismissed semantics).
 */

import { TextAttributes } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { useTerminalDimensions } from "@opentui/solid"
import { For, Show, createSignal } from "solid-js"
import { theme } from "../theme/tokens"
import { moveSelection } from "../util/dialog-select"
import {
  ASK_SKIP,
  planReviewAnswer,
  planReviewItems,
  type AskRequest,
} from "../util/ask"
import { PROMPT_BORDER } from "./prompt"

export function PlanReviewPanel(props: {
  request: AskRequest
  onAnswer: (answer: string) => void
}) {
  const dimensions = useTerminalDimensions()
  const [selected, setSelected] = createSignal(0)
  const [feedback, setFeedback] = createSignal("")
  const options = () =>
    props.request.options.length > 0
      ? props.request.options
      : ["开始执行", "继续规划"]
  const items = () => planReviewItems(props.request)
  const move = (delta: number) =>
    setSelected((current) => moveSelection(current, delta, options().length))
  // Approve submits straight away; keep-planning first lets the feedback be
  // typed (the input row appears with the selection), then enter submits.
  const confirm = () =>
    props.onAnswer(
      selected() === 0
        ? planReviewAnswer("approve")
        : planReviewAnswer("keep", feedback().trim()),
    )

  useBindings(() => ({
    enabled: true,
    commands: [
      { name: "plan-review.prev", desc: "Previous option", run: () => move(-1) },
      { name: "plan-review.next", desc: "Next option", run: () => move(1) },
      { name: "plan-review.confirm", desc: "Answer with the selected option", run: confirm },
      {
        name: "plan-review.skip",
        desc: "Dismiss the review (ends the turn; nothing is submitted)",
        run: () => props.onAnswer(ASK_SKIP),
      },
      {
        name: "plan-review.approve",
        desc: "Approve and execute",
        run: () => props.onAnswer(planReviewAnswer("approve")),
      },
      { name: "plan-review.keep", desc: "Keep planning", run: () => setSelected(1) },
    ],
    bindings: [
      { key: "up", cmd: "plan-review.prev" },
      { key: "k", cmd: "plan-review.prev" },
      { key: "down", cmd: "plan-review.next" },
      { key: "j", cmd: "plan-review.next" },
      { key: "return", cmd: "plan-review.confirm" },
      { key: "escape", cmd: "plan-review.skip" },
      { key: "1", cmd: "plan-review.approve" },
      { key: "2", cmd: "plan-review.keep" },
    ],
  }))

  return (
    <box
      width="100%"
      flexDirection="column"
      border={["left", "bottom"]}
      customBorderChars={PROMPT_BORDER}
      borderColor={theme.actionPrimaryFocused}
      backgroundColor={theme.backgroundBase}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
    >
      <text fg={theme.feedbackWarning} attributes={TextAttributes.BOLD}>
        ◆ Plan review
      </text>
      <text fg={theme.textBase}>{props.request.question}</text>
      {/* R04: the full plan and the item table in one scrolling region. */}
      <scrollbox
        flexShrink={1}
        maxHeight={Math.max(3, Math.floor(dimensions().height / 2) - 6)}
        marginTop={1}
      >
        <text fg={theme.textMuted}>{props.request.detail}</text>
        <box flexDirection="column" marginTop={1}>
          <For each={items()}>
            {(item, index) => (
              <box flexDirection="column">
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
        </box>
      </scrollbox>
      <box flexDirection="column" marginTop={1}>
        <For each={options()}>
          {(option, index) => (
            <box flexDirection="row">
              <text
                fg={index() === selected() ? theme.textBase : theme.textMuted}
                attributes={index() === selected() ? TextAttributes.BOLD : undefined}
              >
                {`${index() === selected() ? "▸" : " "} ${index() + 1}  ${option}`}
              </text>
            </box>
          )}
        </For>
      </box>
      <Show when={selected() === 1}>
        <box flexDirection="row" marginTop={1}>
          <text fg={theme.textMuted}>{"› "}</text>
          <input
            focused
            placeholder="想改什么？直接写，enter 提交（可留空）"
            onInput={(value: string) => setFeedback(value)}
            onSubmit={confirm}
          />
        </box>
      </Show>
      <text fg={theme.textMuted}>↑/↓ select · 1/2 quick · enter confirm · esc 结束本轮（不提交、不执行）</text>
    </box>
  )
}
