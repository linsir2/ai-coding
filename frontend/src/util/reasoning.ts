import { shorten } from "./toolmeta"

/** Long enough for one header line; past this the fold would wrap and break. */
const DEFAULT_CAP = 72

/**
 * The folded "+ Thought" headline (A4 §3): the first sentence of the step's
 * reasoning, so a collapsed thought still says what the model was doing.
 *
 * One rule, no model: take the first non-blank line, stop after the first
 * sentence-ending mark, then `shorten` to the cap.  A full stop only ends a
 * sentence when a space or the line end follows it, so "qwen3.7" and "e.g."
 * survive intact.
 */
export function reasoningHeadline(text: string, cap: number = DEFAULT_CAP): string {
  const line = text.split("\n").map((entry) => entry.trim()).find(Boolean) ?? ""
  if (!line) return ""
  const stop = line.search(/(?:[。！？!?]|[.](?=\s|$))/)
  return shorten(stop >= 0 ? line.slice(0, stop + 1) : line, cap)
}