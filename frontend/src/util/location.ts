/**
 * Pure location helpers (M1).  Semantics are adapted from OpenCode's
 * ui/file-path.tsx (posix-only: this project's shell tool is Linux-only)
 * with `string-width` for display-width math so CJK paths truncate by
 * columns, not by code units.
 */

import path from "node:path"
import stringWidth from "string-width"

const segmenter = new Intl.Segmenter()

/** First (or last, when reverse) grapheme clusters that fit in maxWidth. */
function take(value: string, maxWidth: number, reverse: boolean): string {
  const segments = Array.from(segmenter.segment(value), (item) => item.segment)
  if (reverse) segments.reverse()
  const selected: string[] = []
  let width = 0
  for (const segment of segments) {
    const next = stringWidth(segment)
    if (width + next > maxWidth) break
    selected.push(segment)
    width += next
  }
  if (reverse) selected.reverse()
  return selected.join("")
}

/** A single path segment, cut from the left, keeping its extension. */
function truncateBasename(value: string, maxWidth: number): string {
  if (stringWidth(value) <= maxWidth) return value
  if (maxWidth <= 1) return take("…", maxWidth, false)

  const dot = value.lastIndexOf(".")
  const extension = dot > 0 ? value.slice(dot) : ""
  const extensionWidth = stringWidth(extension)
  if (extensionWidth >= maxWidth) return "…" + take(extension, maxWidth - 1, true)

  const stem = extension ? value.slice(0, dot) : value
  return take(stem, maxWidth - extensionWidth - 1, false) + "…" + extension
}

/**
 * Cut a path to ``maxWidth`` display columns, keeping the deepest (most
 * informative) segments: `"/home/user/projects/myapp"` at 10 -> `"/…/myapp"`.
 */
export function truncatePath(value: string, maxWidth: number): string {
  if (maxWidth <= 0 || !value) return ""
  if (stringWidth(value) <= maxWidth) return value

  const root = value.startsWith("/") ? "/" : ""
  const segments = value.slice(root.length).split("/").filter(Boolean)
  const basename = segments.at(-1) ?? value

  if (segments.length < 2) {
    const rootWidth = stringWidth(root)
    if (rootWidth >= maxWidth) return take(root, maxWidth, false)
    return root + truncateBasename(basename, maxWidth - rootWidth)
  }

  const prefix = `${root}…/`
  const basenameWidth = maxWidth - stringWidth(prefix)
  if (basenameWidth <= 0) return take(prefix, maxWidth, false)

  const compact = truncateBasename(basename, basenameWidth)
  if (compact !== basename) return prefix + compact

  const selected = [basename]
  let width = stringWidth(prefix + basename)
  for (let index = segments.length - 2; index >= 0; index--) {
    const segment = segments[index]!
    const next = stringWidth(segment) + 1
    if (width + next > maxWidth) {
      const available = maxWidth - width - 1
      if (available > 1) selected.unshift(take(segment, available - 1, false) + "…")
      break
    }
    selected.unshift(segment)
    width += next
  }
  return prefix + selected.join("/")
}

/** ``/home/u/proj`` with home ``/home/u`` becomes ``~/proj``; outside stays. */
export function abbreviateHome(input: string, home: string): string {
  if (!home) return input
  const relative = path.relative(home, input)
  if (!relative) return "~"
  if (relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) {
    return input
  }
  return "~/" + relative.split(path.sep).join("/")
}

/** The footer label: ``~/dir:branch`` when the branch is known. */
export function formatLocation(directory: string, branch: string): string {
  return branch ? `${directory}:${branch}` : directory
}
