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

import { Index, Match, Switch } from "solid-js"
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

/**
 * One part, rendered by kind.
 *
 * The branch lives in `<Switch>`/`<Match>`, not in a `switch` statement: the
 * transcript renders parts through `<Index>`, which reuses the instance sitting
 * at a POSITION when the part there changes kind (a streaming text part becomes
 * a reasoning one when `message.finished` lands).  A statement branch is chosen
 * once, at creation, so the reused instance would keep drawing the old kind —
 * which is how a thought ended up rendered as a wall of prose.
 */
function PartView(props: { part: Part; alwaysOpen: boolean }) {
  return (
    <Switch>
      <Match when={props.part.kind === "text" && props.part}>
        {(part) => (
          <box paddingLeft={3} marginTop={1} flexShrink={0}>
            <markdown
              content={part().text}
              streaming={part().streaming}
              syntaxStyle={markdownSyntax}
              fg={theme.textBase}
              bg={theme.backgroundBase}
            />
          </box>
        )}
      </Match>
      <Match when={props.part.kind === "reasoning" && props.part}>
        {(part) => <Reasoning text={part().text} alwaysOpen={props.alwaysOpen} />}
      </Match>
      <Match when={props.part.kind === "tool" && props.part}>
        {(part) => <ToolCard part={part() as Extract<Part, { kind: "tool" }>} />}
      </Match>
      <Match when={props.part.kind === "plan" && props.part}>
        {(part) => (
          <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
            <text fg={theme.textMuted}>{(part() as Extract<Part, { kind: "plan" }>).title}</text>
            <text fg={theme.textMuted}>{(part() as Extract<Part, { kind: "plan" }>).body}</text>
          </box>
        )}
      </Match>
      <Match when={props.part.kind === "notice" && props.part}>
        {(part) => {
          const notice = () => part() as Extract<Part, { kind: "notice" }>
          return (
            <box paddingLeft={3} marginTop={1} flexShrink={0}>
              <text fg={notice().tone === "error" ? theme.feedbackError : theme.textMuted}>
                {notice().text}
              </text>
            </box>
          )
        }}
      </Match>
      <Match when={props.part.kind === "turn-end" && props.part}>
        {(part) => (
          <box paddingLeft={3} marginTop={1} flexShrink={0}>
            <text fg={theme.feedbackError}>{(part() as Extract<Part, { kind: "turn-end" }>).text}</text>
          </box>
        )}
      </Match>
    </Switch>
  )
}

export function MessageView(props: { message: ChatMessage; alwaysOpen: boolean }) {
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
      {(part) => <PartView part={part()} alwaysOpen={props.alwaysOpen} />}
    </Index>
  )
}
