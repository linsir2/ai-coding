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
 *
 * r16/E-2: typing `@` opens the same list over the skill registry; selecting
 * there inserts `@name ` in place of the typed token (it does not submit) —
 * the spelling `resolve_mentions` parses on the backend.
 */

import type { TextareaRenderable } from "@opentui/core"
import { TextAttributes } from "@opentui/core"
import { useBindings, useKeymap } from "@opentui/keymap/solid"
import { For, Show, createEffect, createSignal, onMount, untrack } from "solid-js"
import { theme } from "../theme/tokens"
import { agentAccent } from "../util/agent"
import {
  type PasteOutcome,
  type PromptImage,
  dropToken,
  extractImages,
  hostClipboard,
  imageToken,
  interpretPaste,
} from "../util/attachment"
import { pasteEventOutcome, readClipboardImage } from "../util/clipboard"
import { moveSelection } from "../util/dialog-select"
import type { MentionOption } from "../util/mention"
import { ComposerMeta } from "./composer-meta"
import { filterMentions, mentionQuery, replaceMentionToken } from "../util/mention"
import type { SlashCommand } from "../util/slash"
import { filterSlashCommands, ownCommand, slashQuery, slashSelectionText } from "../util/slash"

/** One row of the suggestion list: `/command` or `@skill`. */
interface Suggestion {
  label: string
  description: string
  run: () => void
}

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
  /** T7: the current agent — names the metadata line and tints the border. */
  agent?: string | null
  /** Display name when the caller resolved one (falls back to the id). */
  agentName?: string | null
  /** 19 §3.1: the session's posture — a suffix on the metadata line. */
  mode?: "normal" | "plan"
  /** P5: the enforcement switch — joins the suffix when set. */
  readonly?: boolean
  /**
   * Submit handler.  Returning ``false`` rejects the submission and keeps the
   * text in the box (busy turns: docs/opencode-gui-m2-plan.md §1).
   */
  onSubmit: (text: string, images: PromptImage[]) => boolean | string | void
  /** Focus the textarea on mount; disable while a turn streams. */
  autofocus?: boolean
  /**
   * D4: a card (permission / ask) owns the keyboard.  The composer stays
   * mounted so the draft survives, but it must not take focus or keypresses --
   * the card's digits, esc and return have to win.
   */
  disabled?: boolean
  /** True while the slash list is up — the session mutes its own bindings. */
  onOverlay?: (visible: boolean) => void
  /**
   * Text to drop into the composer (then clear), e.g. the ``@skill`` seeds
   * the skill/mention lists leave behind.
   */
  seed?: string
  onSeedDone?: () => void
  /** The registry's skills, for the `@` suggestions (r16/E-2). */
  skills?: MentionOption[]
  /** Workspace files/directories for the same list (r17/T4). */
  files?: MentionOption[]
}

export function Prompt(props: PromptProps) {
  const keymap = useKeymap()
  let input: TextareaRenderable | undefined
  const [suggestions, setSuggestions] = createSignal<Suggestion[]>([])
  const [selected, setSelected] = createSignal(0)
  const visible = () => suggestions().length > 0
  // r17/T5, r21: an attached image is a token in the composer's own text, the
  // way OpenCode shows it -- so it sits where it was pasted and backspace
  // removes it like any other character.  This map is only what a token means;
  // the text decides whether it is still attached.
  const [imagesByToken, setImagesByToken] = createSignal<Record<string, string>>({})
  let imageSequence = 0
  const [hint, setHint] = createSignal("")
  const clipboard = hostClipboard()

  // r21: the one place an image joins the message.  The host read takes about
  // two seconds on WSL (it spawns powershell.exe), so the key path says so while
  // it runs -- otherwise a paste looks like it did nothing until the token
  // appears "by itself" a moment later.
  const attach = (dataUrl: string) => {
    imageSequence += 1
    const token = imageToken(imageSequence)
    setImagesByToken((map) => ({ ...map, [token]: dataUrl }))
    input?.insertText(token)
    setHint("")
  }
  const attachFromHost = async (source: "key" | "paste") => {
    if (source === "key") setHint("正在读取剪贴板…")
    const found = await readClipboardImage()
    if ("dataUrl" in found) {
      attach(found.dataUrl)
      return
    }
    setHint(found.error)
  }

  // r22: one keypress should take a whole placeholder, not one character of it.
  // The token is real text, so without this a backspace left "[Imag" behind.
  //
  // Deliberately not opentui's extmarks: that mechanism handles ASCII and
  // corrupts Chinese (verified), and this composer is used in Chinese.
  //
  // The fallback is the textarea's own delete, which was measured byte-for-byte
  // identical to the default key for CJK, ASCII and mixed text -- so intercepting
  // the key costs ordinary editing nothing.
  const dropOne = (direction: "before" | "after") => {
    const fallback = () =>
      direction === "before" ? input?.deleteCharBackward() : input?.deleteChar()
    // A selection is the user's own range; removing it is what they asked for.
    if (input?.editorView?.hasSelection?.()) {
      fallback()
      return
    }
    const result = dropToken(
      input?.plainText ?? "",
      input?.cursorOffset ?? 0,
      new Set(Object.keys(imagesByToken())),
      direction,
    )
    if (!result.dropped) {
      fallback()
      return
    }
    input?.setText(result.text)
    if (input) input.cursorOffset = result.offset
  }

  const paste = async () => {
    // r21: the host clipboard library comes first because it is the library's
    // own path and needs no subprocess -- but it cannot reach a clipboard over
    // SSH and has no fallback when this session has no display server, which is
    // why ctrl+v did nothing on a desktop where OpenCode worked.  OpenCode's
    // own answer (run wl-paste / xclip / osascript / powershell in turn) is the
    // second try, and the message says which path answered.
    const native = await readNativeClipboard()
    if (native.kind === "image") {
      attach(native.dataUrl)
      return
    }
    if (native.kind === "text") {
      input?.insertText(native.text)
      setHint("")
      return
    }
    await attachFromHost("key")
  }

  const readNativeClipboard = async (): Promise<PasteOutcome> => {
    try {
      const result = await clipboard.read({
        preferredTypes: ["image/png", "text/plain"],
        selection: "clipboard",
        signal: AbortSignal.timeout(2000),
      })
      return interpretPaste(result)
    } catch (error) {
      return interpretPaste(null, error instanceof Error ? error : new Error(String(error)))
    }
  }

  const slashSuggestion = (entry: SlashCommand): Suggestion => ({
    label: `/${entry.name}`,
    description: entry.description,
    run: () => {
      // Read the attachments before the text goes: they live in the text now.
      const { images } = extractImages(input?.plainText ?? "", imagesByToken())
      input?.setText("")
      // A text entry (no keymap command) is submitted verbatim: the backend's
      // dispatcher owns what a slash line means (A2.3, SlashCommand's contract).
      const text = slashSelectionText(entry)
      if (text !== undefined) {
        props.onSubmit(text, images)
        return
      }
      keymap.runCommand(entry.command!)
    },
  })

  const mentionSuggestion = (option: MentionOption): Suggestion => ({
    label: option.name.startsWith("@") ? option.name : `@${option.name}`,
    description: option.description ?? "",
    run: () => {
      if (!input) return
      const name = option.name.endsWith("/") ? option.name.slice(0, -1) : option.name
      const inserted = replaceMentionToken(input.plainText, input.cursorOffset, name)
      input.setText(inserted.text)
      input.cursorOffset = inserted.cursor
      input.focus()
    },
  })


  const refresh = () => {
    const text = input?.plainText ?? ""
    const cursor = input?.cursorOffset ?? text.length
    const slash = slashQuery(text, cursor)
    if (slash !== null) {
      const list = filterSlashCommands(slash).map(slashSuggestion)
      setSuggestions(list)
      setSelected((current) => Math.min(current, Math.max(0, list.length - 1)))
      return
    }
    const mention = mentionQuery(text, cursor)
    if (mention !== null) {
      // Skills first: the backend claims a token for a skill before a file, so
      // the list has to read the same way (a name that is both resolves once).
      const seen = new Set<string>()
      const options = [...(props.skills ?? []), ...(props.files ?? [])].filter((option) => {
        const key = option.name.replace(/\/$/, "")
        if (seen.has(key)) return false
        seen.add(key)
        return true
      })
      const list = filterMentions(mention, options).map(mentionSuggestion)
      setSuggestions(list)
      setSelected((current) => Math.min(current, Math.max(0, list.length - 1)))
      return
    }
    setSuggestions([])
  }

  const move = (delta: number) =>
    setSelected((current) => moveSelection(current, delta, suggestions().length))

  const select = () => {
    const choice = suggestions()[selected()]
    if (!choice) return
    setSuggestions([])
    choice.run()
  }

  onMount(() => {
    // Focus after the renderer attached (a microtask is too early: the
    // element exists but the renderer has not wired input routing yet).
    if (props.autofocus !== false && props.disabled !== true) {
      input?.focus()
    }
  })

  // D4: the card that takes the keyboard gives it back when it closes.  The
  // draft is never touched -- that is the whole point of staying mounted.
  let wasDisabled = props.disabled === true
  createEffect(() => {
    const disabled = props.disabled === true
    const element = input
    if (!element) return
    if (disabled) {
      // Renderable.blur() refuses to run once `focusable` is false, so the
      // keyboard has to be handed back before the flag drops -- otherwise the
      // textarea keeps focus (and the card's digits land in the draft).
      element.blur()
      element.focusable = false
    } else {
      element.focusable = true
      if (wasDisabled && props.autofocus !== false) element.focus()
    }
    wasDisabled = disabled
  })

  createEffect(() => {
    const seed = props.seed
    if (!seed) return
    input?.setText(seed)
    // The cursor lands at 0 after setText; a pre-filled command must be
    // *completed* by typing, not prefixed (E2E caught "auto/permissions").
    if (input) input.cursorOffset = seed.length
    // Untracked: only a new seed should re-run this effect.  Reading the prop
    // directly made a card opening replay the seed and overwrite the draft.
    if (untrack(() => props.disabled) !== true) input?.focus()
    props.onSeedDone?.()
  })

  createEffect(() => props.onOverlay?.(visible()))

  useBindings(() => ({
    // Always on while the composer is mounted: paste must work mid-busy too.
    target: () => input,
    commands: [{ name: "prompt.paste", desc: "Paste from the clipboard", run: () => void paste() }],
    bindings: [{ key: "ctrl+v", cmd: "prompt.paste" }],
  }))

  useBindings(() => ({
    // r22: backspace and delete take a whole [Image N] when the cursor is
    // against one, and are otherwise the textarea's own delete (measured
    // identical).  Scoped to focus so the keys are untouched everywhere else.
    target: () => input,
    targetMode: "focus",
    commands: [
      { name: "prompt.backspace", desc: "Delete back", run: () => dropOne("before") },
      { name: "prompt.delete", desc: "Delete forward", run: () => dropOne("after") },
    ],
    bindings: [
      { key: "backspace", cmd: "prompt.backspace" },
      { key: "delete", cmd: "prompt.delete" },
    ],
  }))

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
    const raw = input?.plainText ?? ""
    // r21: the text is the truth.  A token the map knows is an image; one the
    // user deleted (or typed by hand) is just text.
    const { text: messageText, images } = extractImages(raw, imagesByToken())
    const text = messageText.trim()
    if (!text && images.length === 0) return
    setSuggestions([])
    // R20b: a typed line that names one of this TUI's own commands runs that
    // command, exactly as picking it from the palette would.  The backend never
    // heard of /models, /sessions, /new or /mcp, so before this they were
    // submitted as ordinary prose and the model answered a slash line.
    const own = ownCommand(text)
    if (own) {
      input?.setText("")
      setImagesByToken({})
      setHint("")
      keymap.runCommand(own.command!)
      return
    }
    // A string is a refusal with a reason (r18/F14: a command sent no images,
    // so the composer keeps them and says so); false is the busy refusal.
    const refused = props.onSubmit(text, images)
    if (refused === false || typeof refused === "string") {
      if (typeof refused === "string") setHint(refused)
      else if (images.length > 0) setHint("回合进行中：图片不能排队，等这轮结束后再发送")
      return
    }
    setImagesByToken({})
    setHint("")
    input?.setText("")
  }

  return (
    <box
      width="100%"
      border={["left", "bottom"]}
      customBorderChars={PROMPT_BORDER}
        borderColor={agentAccent(props.agent ?? "")}
      backgroundColor={theme.backgroundBase}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
    >
      <box flexDirection="column" width="100%">
        <Show when={hint()}>
          <box flexDirection="column" width="100%">
            <text fg={theme.textMuted}>{hint()}</text>
          </box>
        </Show>
        <Show when={visible()}>
          <box flexDirection="column" width="100%" paddingBottom={1}>
            <For each={suggestions()}>
              {(command, index) => (
                <box flexDirection="row" gap={1}>
                  <text
                    fg={index() === selected() ? theme.actionPrimaryFocused : theme.textBase}
                    attributes={index() === selected() ? TextAttributes.BOLD : undefined}
                  >
                    {command.label.padEnd(14)}
                  </text>
                  <text fg={theme.textMuted}>{command.description}</text>
                </box>
              )}
            </For>
          </box>
        </Show>
        <textarea
          id="composer-input"
          ref={(element: TextareaRenderable) => (input = element)}
          onPaste={(event) => {
            // r21: with bracketed paste on, the terminal eats ctrl+v and hands
            // the app a paste event instead of a keypress -- so the ctrl+v
            // binding below never fires on the terminals most people use.
            const outcome = pasteEventOutcome(event.metadata, event.bytes)
            if (outcome.kind === "text") return
            event.preventDefault()
            if (outcome.kind === "image") {
              attach(outcome.dataUrl)
              return
            }
            // "clipboard": the terminal had nothing to forward, so ask the host.
            void attachFromHost("paste")
          }}
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
        <ComposerMeta
          agent={props.agent}
          agentName={props.agentName}
          mode={props.mode}
          readonly={props.readonly}
          metadata={props.metadata}
        />
      </box>
    </box>
  )
}
