/**
 * The composer — OpenCode's prompt look:
 *
 *   ┃                                      <- left border, agent color
 *   ┃  Ask anything… "Fix a TODO…"         <- textarea (autogrow)
 *   ┃
 *   ┃  Build · model · variant             <- metadata row (muted)
 *   ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀       <- half-block bottom cap
 *
 * The border characters are OpenCode's `SplitBorder` set (┃ ╹ ▀); the box
 * background is one step below the raised surface, exactly like theirs.
 */

import type { TextareaRenderable } from "@opentui/core"
import { onMount } from "solid-js"
import { theme } from "../theme/tokens"

/** OpenCode's prompt border: left-only ┃ with a half-block bottom cap. */
export const PROMPT_BORDER = {
  topLeft: "",
  topRight: "",
  bottomRight: "",
  horizontal: "▀",
  vertical: "┃",
  bottomLeft: "╹",
  topT: "",
  bottomT: "",
  leftT: "",
  rightT: "",
  cross: "",
}

export interface PromptProps {
  placeholder: string
  metadata: string
  onSubmit: (text: string) => void
  /** Focus the textarea on mount; disable while a turn streams. */
  autofocus?: boolean
}

export function Prompt(props: PromptProps) {
  let input: TextareaRenderable | undefined

  onMount(() => {
    // Focus after the renderer attached (a microtask is too early: the
    // element exists but the renderer has not wired input routing yet).
    if (props.autofocus !== false) {
      input?.focus()
    }
  })

  const submit = () => {
    const text = (input?.plainText ?? "").trim()
    if (!text) return
    input?.setText("")
    props.onSubmit(text)
  }

  return (
    <box
      width="100%"
      border={["left", "bottom"]}
      customBorderChars={PROMPT_BORDER}
      borderColor={theme.agentColor}
      backgroundColor={theme.backgroundBase}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
    >
      <box flexDirection="column" width="100%">
        <textarea
          ref={(element: TextareaRenderable) => (input = element)}
          placeholder={props.placeholder}
          placeholderColor={theme.textMuted}
          textColor={theme.textBase}
          focusedTextColor={theme.textBase}
          backgroundColor="transparent"
          focusedBackgroundColor="transparent"
          cursorColor={theme.textBase}
          minHeight={1}
          maxHeight={8}
          // OpenCode's input contract: return submits, shift+return newlines
          // (the renderable's default is the opposite).
          keyBindings={[
            { name: "return", action: "submit" },
            { name: "kpenter", action: "submit" },
            { name: "return", shift: true, action: "newline" },
            { name: "kpenter", shift: true, action: "newline" },
          ]}
          onSubmit={() => {
            // Same IME double-defer OpenCode uses: the last composed character
            // must be flushed to plainText before it is read.
            setTimeout(() => setTimeout(submit, 0), 0)
          }}
        />
        <text fg={theme.textMuted}> </text>
        <text fg={theme.textMuted}>{props.metadata}</text>
      </box>
    </box>
  )
}
