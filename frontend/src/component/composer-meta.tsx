/**
 * The composer's metadata line (19 §3.1) — the mode tag first, then the
 * model/provider line the prompt already carried.  A missing mode (an older
 * backend payload) reads as Build, the same fail-safe direction as the stats
 * contract itself.  Extracted from ``prompt.tsx`` so the frame is testable
 * without the composer's keymap/clipboard machinery (status-bar precedent).
 */

import { TextAttributes } from "@opentui/core"
import { theme } from "../theme/tokens"

export function ComposerMeta(props: {
  mode?: "normal" | "plan"
  metadata: string
}) {
  const plan = () => props.mode === "plan"
  return (
    <box flexDirection="row">
      <text
        fg={plan() ? theme.feedbackWarning : theme.textMuted}
        attributes={plan() ? TextAttributes.BOLD : undefined}
      >
        {plan() ? "PLAN" : "Build"}
      </text>
      <text fg={theme.textMuted}> · </text>
      <text fg={theme.textMuted}>{props.metadata}</text>
    </box>
  )
}
