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

import { ScrollBoxRenderable, TextAttributes } from "@opentui/core"
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

/** C: what the card's keys MEANT -- the only answer-semantics the client owns.
 *
 *  A (audit fix): the scroll region needs a keyboard path.  ``up``/``down``
 *  already move the two options, so scrolling takes the pager keys and the
 *  half-page pair; the footer's hint is generated from this list so the two
 *  can never drift apart. */
export type ReviewIntent = "approve" | "dismiss"

export const SCROLL_STEP = 4

/** A: the keys that scroll the review region, as (key, direction) pairs.
 *
 *  Rendered into the footer's hint and bound to the scrollbox alike, so what
 *  the user is told is exactly what is wired -- they were one commit apart. */
export const REVIEW_SCROLL_KEYS: readonly { key: string; dir: 1 | -1; label: string }[] = [
  { key: "pageup", dir: -1, label: "pgup" },
  { key: "pagedown", dir: 1, label: "pgdn" },
  { key: "ctrl+u", dir: -1, label: "ctrl+u" },
  { key: "ctrl+d", dir: 1, label: "ctrl+d" },
]

export const REVIEW_SCROLL_HINT = REVIEW_SCROLL_KEYS.map((k) => k.label).join("/")

/** The card's key table, exported so the tests drive THIS table (a copy in a
 *  test proves nothing about the card). */
export const REVIEW_KEY_BINDINGS: readonly { key: string; cmd: string }[] = [
  { key: "up", cmd: "plan-review.prev" },
  { key: "k", cmd: "plan-review.prev" },
  { key: "down", cmd: "plan-review.next" },
  { key: "j", cmd: "plan-review.next" },
  { key: "return", cmd: "plan-review.confirm" },
  { key: "escape", cmd: "plan-review.dismiss" },
  { key: "1", cmd: "plan-review.approve" },
  { key: "2", cmd: "plan-review.keep" },
  ...REVIEW_SCROLL_KEYS.map(({ key, dir }) => ({
    key,
    cmd: dir === 1 ? "plan-review.scroll-down" : "plan-review.scroll-up",
  })),
]

export function PlanReviewPanel(props: {
  request: AskRequest
  /**
   * C: the answer and what it MEANT.  The panel knows which button was
   * pressed, so it says so instead of letting the caller re-parse the answer
   * string -- that second judgement (`isPlanApproval`) disagreed with the
   * backend's `_parse_answer` on approve+feedback and would have armed a run
   * the backend refused.  The backend stays the only authority on what gets
   * written; this only tells the client whether to dispatch it.
   */
  onAnswer: (answer: string, intent: ReviewIntent) => void
}) {
  const dimensions = useTerminalDimensions()
  const [selected, setSelected] = createSignal(0)
  const [feedback, setFeedback] = createSignal("")
  // A: the scroll region's handle.  Same contract dialog-select uses (a ref
  // into the scrollbox, its scrollChildIntoView/scrollBy to move it) -- the
  // review card simply had none, which is why a 24-row terminal showed the
  // plan prose and none of the steps approval would write.
  let region: ScrollBoxRenderable | undefined
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
    selected() === 0
      ? props.onAnswer(planReviewAnswer("approve"), "approve")
      : props.onAnswer(planReviewAnswer("keep", feedback().trim()), "dismiss")

  useBindings(() => ({
    enabled: true,
    commands: [
      { name: "plan-review.prev", desc: "Previous option", run: () => move(-1) },
      { name: "plan-review.next", desc: "Next option", run: () => move(1) },
      { name: "plan-review.confirm", desc: "Answer with the selected option", run: confirm },
      {
        name: "plan-review.approve",
        desc: "Approve and execute",
        run: () => props.onAnswer(planReviewAnswer("approve"), "approve"),
      },
      { name: "plan-review.keep", desc: "Keep planning", run: () => setSelected(1) },
      // C: keep is not an approval -- the client must not arm a run for it.
      {
        name: "plan-review.dismiss",
        desc: "Dismiss the review (ends the turn; nothing is submitted)",
        run: () => props.onAnswer(ASK_SKIP, "dismiss"),
      },
      // A: the scroll path.  One command per direction, driven by the exported
      // key table, so the footer hint and the binding cannot drift.
      {
        name: "plan-review.scroll-down",
        desc: "Scroll the plan and items down",
        run: () => region?.scrollBy(SCROLL_STEP),
      },
      {
        name: "plan-review.scroll-up",
        desc: "Scroll the plan and items up",
        run: () => region?.scrollBy(-SCROLL_STEP),
      },
    ],
    bindings: REVIEW_KEY_BINDINGS as never,
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
        ref={(element: ScrollBoxRenderable) => (region = element)}
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
      <text fg={theme.textMuted}>
        {`↑/↓ select · 1/2 quick · enter confirm · ${REVIEW_SCROLL_HINT} 滚动 · esc 结束本轮（不提交、不执行）`}
      </text>
    </box>
  )
}
