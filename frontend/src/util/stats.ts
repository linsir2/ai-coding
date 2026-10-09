/**
 * Token display helpers (M4) — shared by the sidebar Context card and the
 * status bar so both format the same real numbers the same way.
 */

const formatter = new Intl.NumberFormat("en-US")

/** 13930 -> "13,930" (OpenCode's grouping). */
export function formatTokens(tokens: number): string {
  return formatter.format(tokens)
}

/** Rounded 0..100 percentage; a non-positive limit reads as 0. */
export function percentUsed(tokens: number, max: number): number {
  if (max <= 0) return 0
  return Math.min(100, Math.max(0, Math.round((tokens / max) * 100)))
}
