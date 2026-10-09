/**
 * Reasoning part (M2) — OpenCode's hide/show semantics:
 *
 *   hide : a single `+ Thought` line (warning tint, dim)
 *   show : `- Thought` + a left-bordered dim markdown block
 *
 * There is no "Thinking…" spinner state: the backend delivers reasoning
 * complete at step end (no delta stream), so only the settled form exists.
 */

import { theme } from "../theme/tokens"
import { markdownSyntax } from "../theme/syntax"

export function Reasoning(props: { text: string; expanded: boolean }) {
  return (
    <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
      {props.expanded ? (
        <box border={["left"]} borderColor={theme.backgroundRaisedMax} paddingLeft={1} flexShrink={0}>
          <box flexDirection="column">
            <text fg={theme.feedbackWarning}>- Thought</text>
            <markdown content={props.text} syntaxStyle={markdownSyntax} fg={theme.textMuted} bg={theme.backgroundBase} />
          </box>
        </box>
      ) : (
        <text fg={theme.feedbackWarning}>+ Thought</text>
      )}
    </box>
  )
}
