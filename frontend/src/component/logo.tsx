/**
 * Block-art logo, shaped after OpenCode's home logo.
 *
 * Three rows of block glyphs; two color halves (muted left, base right) so the
 * logo reads like theirs.  Adaptive: under 22 columns it degrades to a plain
 * "ai-coding" wordmark, matching OpenCode's small-terminal behavior.
 */

import { theme } from "../theme/tokens"

const GLYPHS: Record<string, [string, string, string]> = {
  A: ["█▀▀█", "█▄▄█", "▀  ▀"],
  I: ["▀█▀", " █ ", "▀▀▀"],
  C: ["█▀▀", "█  ", "▀▀▀"],
  O: ["█▀▀█", "█  █", "▀▀▀▀"],
  D: ["█▀▀▄", "█  █", "▀▀▀▀"],
  N: ["█▄ █", "█ ▀█", "█  █"],
  G: ["█▀▀▀", "█ ▀█", "▀▀▀▀"],
  "-": ["   ", "▀▀▀", "   "],
  " ": [" ", " ", " "],
}

function rowsFor(text: string): [string, string, string] | null {
  const rows: [string, string, string] = ["", "", ""]
  for (const char of text) {
    const glyph = GLYPHS[char]
    if (!glyph) return null
    for (let row = 0; row < 3; row++) {
      rows[row] += (rows[row] ? " " : "") + glyph[row]
    }
  }
  return rows
}

export function Logo(props: { width: number }) {
  const full = () => rowsFor("AI-CODING")
  // Left half muted / right half base: split each row at the space between
  // the words, which is what makes the two-tone read like OpenCode's.
  const splitAt = () => {
    const rows = full()
    if (!rows) return 0
    const index = "AI-CODING".indexOf("-")
    let columns = 0
    for (let i = 0; i <= index; i++) {
      const glyph = GLYPHS["AI-CODING"[i]]
      columns += (glyph?.[0].length ?? 0) + (i > 0 ? 1 : 0)
    }
    return columns
  }
  return (
    <box flexDirection="column">
      {full() && props.width >= 44 ? (
        <>
          {full()!.map((row, i) => (
            <text>
              <span style={{ fg: theme.textMuted }}>{row.slice(0, splitAt())}</span>
              <span style={{ fg: theme.textBase }}>{row.slice(splitAt())}</span>
            </text>
          ))}
        </>
      ) : props.width >= 22 ? (
        <text fg={theme.textBase}>
          <b>ai-coding</b>
        </text>
      ) : (
        <text fg={theme.textMuted}>ai</text>
      )}
    </box>
  )
}
