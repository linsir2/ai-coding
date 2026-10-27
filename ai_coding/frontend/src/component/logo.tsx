/**
 * Block-art logo via figlet (M0.5) — 'ANSI Compact' font, which is a 3-row
 * half-block style in the same visual family as OpenCode's home logo.
 *
 * Rendered as two pieces ("AI" + "CODING") so each half can carry its own
 * color (muted left / base right), matching OpenCode's two-tone logo.
 */

import figlet from "figlet"
import { createMemo } from "solid-js"
import { theme } from "../theme/tokens"

const FONT = "ANSI Compact"

function artRows(text: string): string[] {
  const art = figlet.textSync(text, { font: FONT })
  const rows = art.split("\n").map((row) => row.replace(/\s+$/, ""))
  // figlet pads with blank lines above/below; keep only the glyph rows.
  const first = rows.findIndex((row) => row.trim().length > 0)
  if (first === -1) return []
  let last = rows.length - 1
  while (last > first && rows[last].trim().length === 0) last -= 1
  return rows.slice(first, last + 1)
}

export interface LogoProps {
  width: number
}

export function Logo(props: LogoProps) {
  const rows = createMemo(() => {
    const left = artRows("AI")
    const right = artRows("CODING")
    const height = Math.max(left.length, right.length)
    const leftWidth = Math.max(0, ...left.map((row) => row.length))
    const composed: { left: string; right: string }[] = []
    for (let index = 0; index < height; index++) {
      composed.push({
        left: (left[index] ?? "").padEnd(leftWidth) + "  ",
        right: right[index] ?? "",
      })
    }
    return composed
  })

  return (
    <box flexDirection="column">
      {props.width >= 49 && rows().length > 0 ? (
        rows().map((row) => (
          <text>
            <span style={{ fg: theme.textMuted }}>{row.left}</span>
            <span style={{ fg: theme.textBase }}>{row.right}</span>
          </text>
        ))
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
