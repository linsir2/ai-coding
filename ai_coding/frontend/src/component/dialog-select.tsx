/**
 * DialogSelect (M3) — the reusable list dialog, ported from OpenCode's
 * ui/dialog-select.tsx: a title with an "esc" hint, an auto-focused search
 * box, category-grouped rows, a ● current marker, and keyboard navigation.
 *
 * All decisions live in util/dialog-select.ts (filtering, grouping, wrap
 * navigation) and are unit-tested; this file lays them out and drives the
 * selection.  Rows are rendered from the FLAT list (a category header is
 * emitted when the category changes), so the selected index is the row index.
 */

import { TextAttributes, type InputRenderable, type ScrollBoxRenderable } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { useTerminalDimensions } from "@opentui/solid"
import { Index, Show, createEffect, createMemo, createSignal, onMount } from "solid-js"
import { theme } from "../theme/tokens"
import type { SelectAction, SelectOption } from "../util/dialog-select"
import {
  filterOptions,
  flattenGroups,
  groupOptions,
  moveSelection,
  rowLayout,
  scrollTopFor,
  singleLineText,
} from "../util/dialog-select"

export interface DialogSelectProps {
  title: string
  options: SelectOption[]
  /** Selection handler; falls back to the option's own onSelect. */
  onSelect?: (option: SelectOption) => void
  /** Row actions on the focused option, e.g. delete.  OpenCode's `actions`. */
  actions?: readonly SelectAction[]
  /** Left footer text; defaults to the select hint. */
  footer?: string
  /** Fuzzysort threshold (the palette uses a stricter 0.7). */
  threshold?: number
}

export function DialogSelect(props: DialogSelectProps) {
  const [query, setQuery] = createSignal("")
  const [selected, setSelected] = createSignal(0)
  const dimensions = useTerminalDimensions()
  let input: InputRenderable | undefined
  let list: ScrollBoxRenderable | undefined

  const flat = createMemo(() => flattenGroups(groupOptions(filterOptions(props.options, query(), props.threshold))))
  const showHeader = (index: number): boolean => {
    const options = flat()
    const category = options[index]?.category
    return Boolean(category) && (index === 0 || options[index - 1]?.category !== category)
  }

  const move = (delta: number) => setSelected((current) => moveSelection(current, delta, flat().length))
  const submit = () => {
    const option = flat()[selected()]
    if (option) (props.onSelect ?? option.onSelect)?.(option)
  }

  // R20b: a scrollbox with no explicit height filled the overlay, so an empty
  // list still produced a screen-tall panel.  Size it to the rows it has.
  //
  // The row offsets and the content height come from one layout so the
  // viewport and the scroll math cannot disagree about how tall a row is.
  const layout = createMemo(() => rowLayout(flat().map((_, index) => showHeader(index))))
  const maxList = () => Math.max(3, Math.floor(dimensions().height / 2) - 6)
  const listHeight = () => Math.max(1, Math.min(layout().height, maxList()))

  onMount(() => input?.focus())

  useBindings(() => ({
    commands: [
      { name: "dialog.select.prev", desc: "Previous item", run: () => move(-1) },
      { name: "dialog.select.next", desc: "Next item", run: () => move(1) },
      { name: "dialog.select.page_up", desc: "Page up", run: () => move(-10) },
      { name: "dialog.select.page_down", desc: "Page down", run: () => move(10) },
      { name: "dialog.select.home", desc: "First item", run: () => setSelected(0) },
      { name: "dialog.select.end", desc: "Last item", run: () => setSelected(Math.max(0, flat().length - 1)) },
      { name: "dialog.select.submit", desc: "Select item", run: submit },
      ...(props.actions ?? []).map((action) => ({
        name: `dialog.select.action.${action.title}`,
        desc: action.title,
        run: () => action.onTrigger(flat()[selected()]),
      })),
    ],
    bindings: [
      { key: "up", cmd: "dialog.select.prev" },
      { key: "ctrl+p", cmd: "dialog.select.prev" },
      { key: "down", cmd: "dialog.select.next" },
      { key: "ctrl+n", cmd: "dialog.select.next" },
      { key: "pageup", cmd: "dialog.select.page_up" },
      { key: "pagedown", cmd: "dialog.select.page_down" },
      { key: "home", cmd: "dialog.select.home" },
      { key: "end", cmd: "dialog.select.end" },
      { key: "return", cmd: "dialog.select.submit" },
      ...(props.actions ?? []).map((action) => ({
        key: action.key,
        cmd: `dialog.select.action.${action.title}`,
      })),
    ],
  }))

  // Keep the selected row in view by its content offset.  ScrollBox's
  // scrollChildIntoView gives up when the target is entirely outside the
  // viewport (its nearest-delta returns 0), which is exactly a PageDown /
  // PageUp jump -- the list used to stay put while the selection moved off
  // screen.
  createEffect(() => {
    const index = selected()
    const rowTop = layout().tops[index]
    if (rowTop === undefined || !list) return
    // Scrolling up should reveal the group header too, not half of it.
    const blockTop = showHeader(index) ? Math.max(0, rowTop - 1) : rowTop
    const viewport = Math.max(1, list.viewport.height)
    list.scrollTop = scrollTopFor(list.scrollTop, rowTop, viewport, blockTop)
  })

  return (
    <box flexDirection="column" width="100%" paddingLeft={4} paddingRight={4} paddingBottom={1} gap={1}>
      <box flexDirection="row" justifyContent="space-between">
        <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
          {props.title}
        </text>
        <text fg={theme.textMuted}>esc</text>
      </box>
      <input
        ref={(element: InputRenderable) => (input = element)}
        placeholder="Search"
        placeholderColor={theme.textMuted}
        textColor={theme.textBase}
        focusedTextColor={theme.textBase}
        backgroundColor="transparent"
        focusedBackgroundColor="transparent"
        cursorColor={theme.textBase}
        onInput={(value: string) => {
          setQuery(value)
          setSelected(0)
        }}
      />
      <Show
        when={flat().length > 0}
        fallback={<text fg={theme.textMuted}>{"  no matches"}</text>}
      >
        <scrollbox
          ref={(element: ScrollBoxRenderable) => (list = element)}
          height={listHeight()}
          flexShrink={1}
        >
          <Index each={flat()}>
            {(option, index) => (
              <>
                <Show when={showHeader(index)}>
                  <box paddingTop={index === 0 ? 0 : 1} paddingLeft={2}>
                    <text fg={theme.hueAccent} attributes={TextAttributes.BOLD}>
                      {option().category}
                    </text>
                  </box>
                </Show>
                {/* A row is one terminal line.  Left to flex, a row whose
                    title + description + footer exceed the width was measured
                    two lines tall by the native renderer (the second line
                    blank), halving how many commands fit the viewport. */}
                <box
                  id={`dialog-row-${option().value}`}
                  flexDirection="row"
                  height={1}
                  paddingLeft={2}
                  paddingRight={2}
                  backgroundColor={index === selected() ? theme.backgroundRaisedMax : undefined}
                >
                  <text fg={theme.hueInteractive}>{option().current ? "● " : "  "}</text>
                  <text
                    flexGrow={1}
                    wrapMode="none"
                    fg={option().danger ? theme.feedbackError : index === selected() ? theme.textBase : theme.textMuted}
                    attributes={index === selected() || option().danger ? TextAttributes.BOLD : undefined}
                  >
                    {singleLineText(option().title)}
                    <Show when={option().description}>
                      <span>{`  ${singleLineText(option().description ?? "")}`}</span>
                    </Show>
                  </text>
                  <Show when={option().footer}>
                    <text wrapMode="none" fg={theme.textMuted}>{`  ${option().footer}`}</text>
                  </Show>
                </box>
              </>
            )}
          </Index>
        </scrollbox>
      </Show>
      <text fg={theme.textMuted}>{props.footer ?? "enter select · esc close"}</text>
    </box>
  )
}
