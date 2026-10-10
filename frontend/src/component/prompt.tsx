/**
 * The composer — OpenCode's prompt look:
 *
 *   ┃                                      <- left border, agent color
 *   ┃  /command suggestions                 <- autocomplete list (M5)
 *   ┃  Ask anything… "Fix a TODO…"         <- textarea (autogrow)
 *   ┃
 *   ┃  Build · model · variant             <- metadata row (muted)
 *   ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀       <- half-block bottom cap
 *
 * The border characters are OpenCode's `SplitBorder` set (┃ ╹ ▀); the box
 * background is one step below the raised surface, exactly like theirs.
 *
 * M5: typing `/` opens the autocomplete.  A keymap layer consumes
 * up/down/return/escape while the list is up (dispatched bindings stop the
 * event from reaching the textarea), so return picks a command instead of
 * submitting.  Selection runs a keymap command — never model text.
 */

import type { TextareaRenderable } from "@opentui/core"
import { TextAttributes } from "@opentui/core"
import { useBindings, useKeymap } from "@opentui/keymap/solid"
import { For, Show, createEffect, createSignal, onMount } from "solid-js"
import { theme } from "../theme/tokens"
import { moveSelection } from "../util/dialog-select"
import type { SlashCommand } from "../util/slash"
import { filterSlashCommands, slashQuery, slashSelectionText } from "../util/slash"

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
  /**
   * Submit handler.  Returning ``false`` rejects the submission and keeps the
   * text in the box (busy turns: docs/opencode-gui-m2-plan.md §1).
   */
  onSubmit: (text: string) => boolean | void
  /** Focus the textarea on mount; disable while a turn streams. */
  autofocus?: boolean
  /** True while the slash list is up — the session mutes its own bindings. */
  onOverlay?: (visible: boolean) => void
  /**
   * Text to drop into the composer (then clear).  The slash list's bare
   * ``/plan`` pre-fills "/plan " so the user types the topic and submits.
   */
  seed?: string
  onSeedDone?: () => void
}

export function Prompt(props: PromptProps) {
  const keymap = useKeymap()
  let input: TextareaRenderable | undefined
  const [suggestions, setSuggestions] = createSignal<SlashCommand[]>([])
  const [selected, setSelected] = createSignal(0)
  const visible = () => suggestions().length > 0

  const refresh = () => {
    const text = input?.plainText ?? ""
    const query = slashQuery(text, input?.cursorOffset ?? text.length)
    if (query === null) {
      setSuggestions([])
      return
    }
    const list = filterSlashCommands(query)
    setSuggestions(list)
    setSelected((current) => Math.min(current, Math.max(0, list.length - 1)))
  }

  const move = (delta: number) =>
    setSelected((current) => moveSelection(current, delta, suggestions().length))

  const select = () => {
    const choice = suggestions()[selected()]
    if (!choice) return
    setSuggestions([])
    input?.setText("")
    // A text entry (no keymap command) is submitted verbatim: the backend's
    // dispatcher owns what a slash line means (A2.3, SlashCommand's contract).
    const text = slashSelectionText(choice)
    if (text !== undefined) {
      props.onSubmit(text)
      return
    }
    keymap.runCommand(choice.command!)
  }

  onMount(() => {
    // Focus after the renderer attached (a microtask is too early: the
    // element exists but the renderer has not wired input routing yet).
    if (props.autofocus !== false) {
      input?.focus()
    }
  })

  createEffect(() => {
    const seed = props.seed
    if (!seed) return
    input?.setText(seed)
    // The cursor lands at 0 after setText; a pre-filled command must be
    // *completed* by typing, not prefixed (E2E caught "auto/permissions").
    if (input) input.cursorOffset = seed.length
    input?.focus()
    props.onSeedDone?.()
  })

  createEffect(() => props.onOverlay?.(visible()))

  useBindings(() => ({
    enabled: visible(),
    target: () => input,
    commands: [
      { name: "prompt.autocomplete.prev", desc: "Previous suggestion", run: () => move(-1) },
      { name: "prompt.autocomplete.next", desc: "Next suggestion", run: () => move(1) },
      {
        name: "prompt.autocomplete.hide",
        desc: "Hide suggestions",
        run: () => setSuggestions([]),
      },
      { name: "prompt.autocomplete.select", desc: "Run the selected command", run: select },
    ],
    bindings: [
      { key: "up", cmd: "prompt.autocomplete.prev" },
      { key: "down", cmd: "prompt.autocomplete.next" },
      { key: "tab", cmd: "prompt.autocomplete.select" },
      { key: "return", cmd: "prompt.autocomplete.select" },
      { key: "escape", cmd: "prompt.autocomplete.hide" },
    ],
  }))

  const submit = () => {
    const text = (input?.plainText ?? "").trim()
    if (!text) return
    setSuggestions([])
    if (props.onSubmit(text) === false) return
    input?.setText("")
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
        <Show when={visible()}>
          <box flexDirection="column" width="100%" paddingBottom={1}>
            <For each={suggestions()}>
              {(command, index) => (
                <box flexDirection="row" gap={1}>
                  <text
                    fg={index() === selected() ? theme.actionPrimaryFocused : theme.textBase}
                    attributes={index() === selected() ? TextAttributes.BOLD : undefined}
                  >
                    {`/${command.name}`.padEnd(10)}
                  </text>
                  <text fg={theme.textMuted}>{command.description}</text>
                </box>
              )}
            </For>
          </box>
        </Show>
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
          onContentChange={refresh}
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
