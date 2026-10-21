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

import { For, Index, Match, Switch, Show } from "solid-js"
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
        {(part) => {
          // 20 §批次2: one in-place card, OpenCode's checkbox grammar. The
          // markers live here (not task.py's `_MARKERS`) because that table
          // is prompt format; the UI's job is the glyph a reader scans.
          const plan = () => part() as Extract<Part, { kind: "plan" }>
          const done = () => plan().items.filter((item) => item.status === "completed").length
          const marker = (status: string) =>
            status === "completed"
              ? "[✓]"
              : status === "in_progress"
                ? "[•]"
                : status === "blocked"
                  ? "[!]"
                  : status === "dropped"
                    ? "[−]"
                    : "[ ]"
          const tone = (status: string) =>
            status === "completed" || status === "dropped"
              ? theme.textMuted
              : status === "in_progress"
                ? theme.feedbackWarning
                : status === "blocked"
                  ? theme.feedbackError
                  : theme.textBase
          return (
            <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
              <text fg={theme.textMuted}>{`# Todos ${done()}/${plan().items.length}`}</text>
              <Show when={plan().goal}>
                <text fg={theme.textMuted}>{`目标: ${plan().goal}`}</text>
              </Show>
              <For each={plan().items}>
                {(item) => (
                  <>
                    <text fg={tone(item.status)}>
                      {`${marker(item.status)} ${item.id}. ${item.content}`}
                    </text>
                    {/* D5: the second line the review card draws -- what
                      approval will run, and what it waits on. Always shown
                      (an empty proof reads "无证明命令", never a missing
                      line), so the two cards check against each other. */}
                    <text fg={theme.textMuted}>
                      {`   verify：${item.verify || "无证明命令"}` +
                        (item.dependsOn.length > 0
                          ? ` · 依赖 ${item.dependsOn.map((d) => `#${d}`).join(", ")}`
                          : "")}
                    </text>
                  </>
                )}
              </For>
            </box>
          )
        }}
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
      <Match when={props.part.kind === "image" && props.part}>
        {(part) => {
          const image = () => part() as Extract<Part, { kind: "image" }>
          return (
            <box paddingLeft={3} flexShrink={0}>
              <text fg={theme.textMuted}>{`[Image: ${image().name}]`}</text>
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
      <Match when={props.part.kind === "compaction" && props.part}>
        {(part) => {
          const row = () => part() as Extract<Part, { kind: "compaction" }>
          const reasonLabel =
            row().reason === "manual" ? "手动" : row().reason === "overflow" ? "溢出" : "自动"
          return (
            <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
              {row().status === "running" ? (
                <text fg={theme.textMuted}>◇ 正在压缩（{reasonLabel}）…</text>
              ) : row().status === "failed" ? (
                <text fg={theme.feedbackError}>✗ 压缩失败：{row().error}</text>
              ) : row().status === "skipped" ? (
                <text fg={theme.textMuted}>○ 无可压缩内容</text>
              ) : (
                <text fg={theme.textMuted}>── 压缩 ──</text>
              )}
              {row().text ? (
                <markdown
                  content={row().text}
                  streaming={row().status === "running"}
                  syntaxStyle={markdownSyntax}
                  fg={theme.textBase}
                  bg={theme.backgroundBase}
                />
              ) : null}
              {row().status === "done" ? (
                <text fg={theme.textMuted}>
                  · {row().beforeMessages}→{row().afterMessages} 条 · {row().inputTokens} in ·{" "}
                  {row().outputTokens} out · 压缩耗 {row().totalTokens}
                </text>
              ) : null}
            </box>
          )
        }}
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
