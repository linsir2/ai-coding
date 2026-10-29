/**
 * DialogSelect pure logic (M3) — filtering, grouping, navigation.
 *
 * The port of OpenCode's ui/dialog-select.tsx behaviour that does not need a
 * renderer: fuzzysort over title/category/description (title weighted 2x),
 * category grouping, and wrap-around selection math.  The component renders
 * what these decide.
 */

import { go } from "fuzzysort"

export interface SelectOption {
  title: string
  value: string
  description?: string
  category?: string
  footer?: string
  current?: boolean
  /** Destructive: the row is armed and waiting for the second press. */
  danger?: boolean
  onSelect?: () => void
}

/**
 * A key-actuated action.  It receives the focused row when there is one; a
 * panel-level action (the MCP dialog's "connect a new server", which matters
 * most when the list is *empty*) takes none and ignores it — which is why the
 * parameter is optional rather than the call being guarded by a row existing.
 */
export interface SelectAction {
  key: string
  title: string
  onTrigger: (option?: SelectOption) => void
}

/** Default fuzzysort threshold; the palette uses a stricter 0.7. */
const DEFAULT_THRESHOLD = 0.5

/** Empty query keeps the list as-is; otherwise fuzzy-match, best first. */
export function filterOptions(
  options: SelectOption[],
  query: string,
  threshold: number = DEFAULT_THRESHOLD,
): SelectOption[] {
  const needle = query.trim()
  if (!needle) return options
  return go(needle, options, {
    keys: ["title", "category", "description"],
    scoreFn: (result) => result[0]!.score * 2 + result[1]!.score + result[2]!.score,
    threshold,
  }).map((result) => result.obj)
}

/** Group by `category` in first-seen order; unlabelled options share "". */
export function groupOptions(
  options: SelectOption[],
): { category: string; options: SelectOption[] }[] {
  const groups: { category: string; options: SelectOption[] }[] = []
  const byCategory = new Map<string, { category: string; options: SelectOption[] }>()
  for (const option of options) {
    const category = option.category ?? ""
    let group = byCategory.get(category)
    if (!group) {
      group = { category, options: [] }
      byCategory.set(category, group)
      groups.push(group)
    }
    group.options.push(option)
  }
  return groups
}

/** The navigation order: group by group, option by option. */
export function flattenGroups(
  groups: { category: string; options: SelectOption[] }[],
): SelectOption[] {
  return groups.flatMap((group) => group.options)
}

/** Move the selection by `delta`, wrapping around both ends. */
export function moveSelection(current: number, delta: number, count: number): number {
  if (count <= 0) return 0
  return (((current + delta) % count) + count) % count
}

/**
 * Content line offsets for the rows DialogSelect lays out, and the total
 * content height in lines.
 *
 * A category header occupies one line for the first group and two for later
 * ones (the label box carries a one-line top gap); every option is one line.
 * The render, the viewport height, and the scroll math all derive from this
 * single layout, so none of them can drift from the others.
 */
export function rowLayout(startsHeader: readonly boolean[]): { tops: number[]; height: number } {
  const tops: number[] = []
  let line = 0
  startsHeader.forEach((header, index) => {
    if (header) {
      if (index > 0) line += 1
      line += 1
    }
    tops.push(line)
    line += 1
  })
  return { tops, height: line }
}

/**
 * Collapse a multi-line label to one line.
 *
 * Rows are one terminal line tall, and queued-input / memory labels can carry
 * newlines from the user's own text.  Folding them keeps the words (instead of
 * silently dropping every line after the first) while the row stays a row.
 */
export function singleLineText(text: string): string {
  return text.replace(/\s*\n\s*/g, " ")
}

/**
 * Scroll offset that brings a one-line row at `rowTop` into a `viewport`-line
 * window starting at `currentTop`, with the smallest move.
 *
 * `blockTop` is where the row's group header starts (the first option of a
 * category); scrolling up aligns to it so Home/PageUp do not leave a header
 * sliced off the top.  It defaults to the row itself.
 *
 * This replaces ScrollBox.scrollChildIntoView, whose nearest-delta helper
 * returns 0 when the target row is entirely outside the viewport -- exactly
 * the PageDown/PageUp case, where the selection jumps past the window and the
 * list stopped following it.
 */
export function scrollTopFor(
  currentTop: number,
  rowTop: number,
  viewport: number,
  blockTop: number = rowTop,
): number {
  const height = Math.max(1, viewport)
  if (rowTop < currentTop) return Math.max(0, blockTop)
  if (rowTop + 1 > currentTop + height) return Math.max(0, rowTop + 1 - height)
  return currentTop
}
