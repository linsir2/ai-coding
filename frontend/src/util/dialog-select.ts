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

/** A key-actuated action on the focused row (OpenCode's `actions`). */
export interface SelectAction {
  key: string
  title: string
  onTrigger: (option: SelectOption) => void
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
