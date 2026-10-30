/**
 * Ask panel (M6.5) — the model's question, shown in the prompt's slot while
 * the ``ask_user`` tool waits for a human.
 *
 * The answer travels back as the result of the server's ``user_prompt.ask``
 * (app.tsx registers the request handler) — the plain text the tool renders
 * as ``用户回答：…``.  ↑/↓ (or j/k) pick an option, 1-9 jump straight to one,
 * enter answers; without options the panel holds a one-line input for a typed
 * answer; esc answers with the empty string, which the shared callback turns
 * into ``reason="empty"`` so the model decides itself instead of hanging.
 *
 * C3: the choices are the model's, so they can never cover everyone.  A list
 * therefore always carries one extra row — 「其他（自己填写）」 — which opens
 * the same input the option-less panel uses.  A question is a place to answer,
 * not a menu to squeeze the human into.
 */

import { TextAttributes } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { For, Show, createSignal } from "solid-js"
import { theme } from "../theme/tokens"
import { moveSelection } from "../util/dialog-select"
import { answerForSelection, type AskRequest } from "../util/ask"
import { PROMPT_BORDER } from "./prompt"

/** C3: the row that opens the free-text input (the model's options cannot be complete). */
export const ASK_OTHER_OPTION = "其他（自己填写）"

export function AskPanel(props: {
  request: AskRequest
  onAnswer: (answer: string) => void
}) {
  const [selected, setSelected] = createSignal(0)
  const [typed, setTyped] = createSignal("")
  const options = () => props.request.options
  // C3: with choices on offer, the free-text row is appended to them.
  const rows = () => (options().length > 0 ? [...options(), ASK_OTHER_OPTION] : [])
  const onOther = () => options().length > 0 && selected() === options().length

  const move = (delta: number) =>
    setSelected((current) => moveSelection(current, delta, Math.max(1, rows().length)))
  const confirm = () => {
    if (options().length === 0) {
      props.onAnswer(typed().trim())
      return
    }
    if (onOther()) {
      // An empty free-text answer is not "skip": it means the user is still
      // typing.  esc stays the only way to let the model decide.
      const text = typed().trim()
      if (text) props.onAnswer(text)
      return
    }
    props.onAnswer(answerForSelection(options(), selected()))
  }
  const quick = (index: number) => {
    // Only the model's own choices get number keys; the free-text row is
    // reached with ↓/↓ or by pressing its number last.
    if (index < options().length) {
      props.onAnswer(answerForSelection(options(), index))
      return
    }
    if (index === options().length) setSelected(index)
  }

  useBindings(() => ({
    enabled: true,
    commands: [
      { name: "ask.prev", desc: "Previous option", run: () => move(-1) },
      { name: "ask.next", desc: "Next option", run: () => move(1) },
      { name: "ask.confirm", desc: "Answer with the selected option", run: confirm },
      { name: "ask.skip", desc: "Let the model decide", run: () => props.onAnswer("") },
      ...Array.from({ length: 9 }, (_unused, index) => ({
        name: `ask.option.${index + 1}`,
        desc: `Answer with option ${index + 1}`,
        run: () => quick(index),
      })),
    ],
    bindings: [
      { key: "up", cmd: "ask.prev" },
      { key: "k", cmd: "ask.prev" },
      { key: "down", cmd: "ask.next" },
      { key: "j", cmd: "ask.next" },
      { key: "return", cmd: "ask.confirm" },
      { key: "escape", cmd: "ask.skip" },
      ...Array.from({ length: 9 }, (_unused, index) => ({
        key: String(index + 1),
        cmd: `ask.option.${index + 1}`,
      })),
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
        ◆ Question
      </text>
      <text fg={theme.textBase}>{props.request.question}</text>
      <Show when={options().length > 0} fallback={
        <box flexDirection="row" marginTop={1}>
          <text fg={theme.textMuted}>{"› "}</text>
          <input
            focused
            placeholder="Type your answer, or esc to let the model decide"
            onInput={(value: string) => setTyped(value)}
            onSubmit={() => props.onAnswer(typed().trim())}
          />
        </box>
      }>
        <box flexDirection="column" marginTop={1}>
          <For each={rows()}>
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
        <Show when={onOther()}>
          <box flexDirection="row" marginTop={1}>
            <text fg={theme.textMuted}>{"› "}</text>
            <input
              focused
              placeholder="写下你的答案，enter 提交（可留空，esc 让模型自行决定）"
              onInput={(value: string) => setTyped(value)}
              onSubmit={() => {
                const text = typed().trim()
                if (text) props.onAnswer(text)
              }}
            />
          </box>
        </Show>
      </Show>
      <text fg={theme.textMuted}>
        {options().length > 0
          ? "↑/↓ select · 1-9 quick · enter answer · esc skip"
          : "enter answer with what you typed · esc skip"}
      </text>
    </box>
  )
}
