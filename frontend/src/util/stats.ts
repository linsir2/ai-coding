/**
 * Token display helpers (M4) — shared by the sidebar Context card and the
 * status bar so both format the same real numbers the same way.
 */

const formatter = new Intl.NumberFormat("en-US")

/** 13930 -> "13,930" (OpenCode's grouping). */
export function formatTokens(tokens: number): string {
  return formatter.format(tokens)
}

/**
 * Rounded percentage; a non-positive limit reads as 0.
 *
 * Not clamped to 100. Measured live: `max_context_tokens` was set to 48,000
 * against a model whose real window is 256k, so the meter reported 237,157
 * tokens as a flat "100% used" — and a full window that still accepted
 * requests looks exactly like a bug. Showing 494% says plainly that the
 * configured window disagrees with the model, which is the truth and is
 * actionable.
 */
export function percentUsed(tokens: number, max: number): number {
  if (max <= 0) return 0
  return Math.max(0, Math.round((tokens / max) * 100))
}
