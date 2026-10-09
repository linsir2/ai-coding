/**
 * Message rendering (M2) — OpenCode's transcript grammar:
 *
 *   user      -> left ┃ border in agent color, raised background
 *   assistant -> parts, each with its own shape:
 *                  text      <markdown streaming> at paddingLeft 3
 *                  reasoning + Thought (ctrl+t expands)
 *                  tool      <ToolCard>
 *                  plan      dim title + body
 *                  notice    dim (or red) one-liner
 *                  turn-end  red one-liner
 *
 * The `kind` switch runs once per slot (parts never change kind in place —
 * the reducer replaces the object at the same index), while the JSX reads
 * `props.part.*` through Solid's compiled getters so in-place updates stay
 * reactive without remounting.
 */

import { Index } from "solid-js"
import type { ChatMessage, Part } from "../state/session"
import { theme } from "../theme/tokens"
import { markdownSyntax } from "../theme/syntax"
import { Reasoning } from "./reasoning"
import { ToolCard } from "./toolcard"

/** OpenCode's user-message border: left ┃ with a half-block bottom cap. */
export const USER_BORDER = {
  topLeft: "",
  topRight: "",
  bottomRight: "",
  horizontal: "▀",
  vertical: "┃",
  bottomLeft: "╹",
  topT: "",
  bottomT: "",
  leftT: "",
  rightT: "",
  cross: "",
}

function PartView(props: { part: Part; thinking: boolean }) {
  switch (props.part.kind) {
    case "text":
      return (
        <box paddingLeft={3} marginTop={1} flexShrink={0}>
          <markdown
            content={props.part.text}
            streaming={props.part.streaming}
            syntaxStyle={markdownSyntax}
            fg={theme.textBase}
            bg={theme.backgroundBase}
          />
        </box>
      )
    case "reasoning":
      return <Reasoning text={props.part.text} expanded={props.thinking} />
    case "tool":
      return <ToolCard part={props.part} />
    case "plan":
      return (
        <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
          <text fg={theme.textMuted}>{props.part.title}</text>
          <text fg={theme.textMuted}>{props.part.body}</text>
        </box>
      )
    case "notice":
      return (
        <box paddingLeft={3} marginTop={1} flexShrink={0}>
          <text fg={props.part.tone === "error" ? theme.feedbackError : theme.textMuted}>
            {props.part.text}
          </text>
        </box>
      )
    case "turn-end":
      return (
        <box paddingLeft={3} marginTop={1} flexShrink={0}>
          <text fg={theme.feedbackError}>{props.part.text}</text>
        </box>
      )
  }
}

export function MessageView(props: { message: ChatMessage; thinking: boolean }) {
  if (props.message.role === "user") {
    const text = () => {
      const part = props.message.parts[0]
      return part?.kind === "text" ? part.text : ""
    }
    return (
      <box
        marginTop={1}
        border={["left"]}
        customBorderChars={USER_BORDER}
        borderColor={theme.agentColor}
        backgroundColor={theme.backgroundRaised}
        paddingLeft={2}
        paddingRight={2}
        flexShrink={0}
      >
        <text fg={theme.textBase}>{text()}</text>
      </box>
    )
  }
  return (
    <Index each={props.message.parts}>
      {(part) => <PartView part={part()} thinking={props.thinking} />}
    </Index>
  )
}
