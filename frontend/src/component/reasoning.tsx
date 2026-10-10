/**
 * Reasoning part (M2, per-part fold in A4) — OpenCode's hide/show semantics:
 *
 *   folded  : one line, "+ Thought" plus the first sentence of the thinking
 *   open    : "- Thought" + a left-bordered dim markdown block
 *
 * The fold belongs to the PART, not the view: `message-parts.tsx:52` keeps one
 * `expanded` signal per reasoning block and toggles it on `onMouseUp`, so two
 * thoughts in one turn open and close independently.  `alwaysOpen` is the
 * view-level mode (our `ctrl+t`) that pins every block open and turns the
 * individual toggle off — the same split as OpenCode's `inMinimal()`.
 *
 * There is no "Thinking…" spinner state: the backend delivers reasoning
 * complete at step end (no delta stream), so only the settled form exists.
 */

import { Show, createSignal } from "solid-js"
import { theme } from "../theme/tokens"
import { markdownSyntax } from "../theme/syntax"
import { reasoningHeadline } from "../util/reasoning"

export function Reasoning(props: { text: string; alwaysOpen: boolean }) {
  const [expanded, setExpanded] = createSignal(false)
  const open = () => props.alwaysOpen || expanded()
  const toggle = () => {
    if (!props.alwaysOpen) setExpanded((value) => !value)
  }
  const headline = () => (open() ? "" : reasoningHeadline(props.text))

  return (
    <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
      {open() ? (
        <box border={["left"]} borderColor={theme.backgroundRaisedMax} paddingLeft={1} flexShrink={0}>
          <box flexDirection="column">
            {/* Both states carry the toggle, so an open thought closes again
                (OpenCode binds it on the header in both branches too). */}
            <text fg={theme.feedbackWarning} onMouseUp={toggle}>
              - Thought
            </text>
            <markdown content={props.text} syntaxStyle={markdownSyntax} fg={theme.textMuted} bg={theme.backgroundBase} />
          </box>
        </box>
      ) : (
        <box flexDirection="row">
          <text fg={theme.feedbackWarning} onMouseUp={toggle}>
            + Thought
          </text>
          <Show when={headline()}>
            <text fg={theme.textMuted} onMouseUp={toggle}>{`  ${headline()}`}</text>
          </Show>
        </box>
      )}
    </box>
  )
}