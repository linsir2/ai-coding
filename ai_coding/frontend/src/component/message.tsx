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
import type { ChatMessage, LayerStat, Part } from "../state/session"
import { theme } from "../theme/tokens"
import { agentAccent } from "../util/agent"
import { markdownSyntax } from "../theme/syntax"
import { Reasoning } from "./reasoning"
import { ToolCard } from "./toolcard"

/**
 * What each pipeline layer does, in one word (R22/A-8).
 *
 * The compaction line used to read `L3 681→681 tok / L1 … / L2 …` — numbers with
 * no meaning for anyone who has not read the pipeline: L1 snips messages, L2
 * replaces a body with a pointer, L3 persists a large output to a file, L4
 * summarises.  The label rides on the layer that reports it, so no extra line
 * and no legend to learn.
 */
const LAYER_LABELS: Record<string, string> = {
  L1: "剪断",
  L2: "占位",
  L3: "落盘",
  L4: "摘要",
}

/** First line of a multi-line detail, clipped to ``limit`` characters. */
function oneLine(text: string, limit: number): string {
  const first = (text || "").split("\n")[0] ?? ""
  return first.length <= limit ? first : `${first.slice(0, limit)}…`
}

/** One layer's share of the compaction line (M-E3): counts + token delta.

 * Counts alone would mislead for L2/L3 — they rewrite a body and keep the
 * message — so the token transition rides along, matching `/trace`.
 */
export function layerLine(layer: LayerStat): string {
  const tail = `${layer.beforeTokens}→${layer.afterTokens} tok`
  const name = `${layer.name} ${LAYER_LABELS[layer.name] ?? ""}`.trim()
  if (layer.name === "L4") return `${name} ${tail}`
  if (layer.removed) return `${name} −${layer.removed} 条 · ${tail}`
  const bits: string[] = []
  if (layer.rewritten) bits.push(`改写 ${layer.rewritten} 条`)
  if (layer.skipped) bits.push(`跳过 ${layer.skipped} 条`)
  bits.push(tail)
  return `${name} ${bits.join(" · ")}`
}

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
          const skipped = () => plan().items.filter((item) => item.status === "skipped").length
          // C3: a live wave names itself (completed waves + 1); between waves
          // the card keeps the plain count, so a finished plan reads as before.
          const waveLabel = () => {
            const wave = plan().wave
            const rounds = plan().rounds ?? 0
            if (wave) {
              const ids = wave.items.length > 0 ? ` · #${wave.items.join(",#")} 进行中` : ""
              return ` · 第 ${rounds + 1} 波${ids}`
            }
            return rounds > 0 ? ` · ${rounds} 波` : ""
          }
          const marker = (status: string) =>
            status === "completed"
              ? "[✓]"
              : status === "in_progress"
                ? "[•]"
                : status === "blocked"
                  ? "[!]"
                  : status === "dropped"
                    ? "[−]"
                    : status === "skipped"
                      ? "[~]"
                      : "[ ]"
          const tone = (status: string) =>
            status === "completed" || status === "dropped" || status === "skipped"
              ? theme.textMuted
              : status === "in_progress"
                ? theme.feedbackWarning
                : status === "blocked"
                  ? theme.feedbackError
                  : theme.textBase
          return (
            <box paddingLeft={3} marginTop={1} flexShrink={0} flexDirection="column">
              <text fg={theme.textMuted}>
                {`# Todos ${done()}/${plan().items.length}` +
                  (skipped() ? ` · 跳过 ${skipped()}` : "") +
                  waveLabel()}
              </text>
              <Show when={plan().goal}>
                <text fg={theme.textMuted}>{`目标: ${plan().goal}`}</text>
              </Show>
              <For each={plan().items}>
                {(item) => (
                  <>
                    <text fg={tone(item.status)}>
                      {`${marker(item.status)} ${item.id}. ${item.content}`}
                    </text>
                    {/* The proof command is a review-time fact (the approval card
                      draws it in full).  In the live card it is noise for every
                      settled item, so it appears only for the one item running
                      now, and truncated.  A blocked item shows why instead --
                      that is the line a reader actually needs. */}
                    <Show when={item.status === "in_progress" && item.verify}>
                      <text fg={theme.textMuted}>
                        {`   verify: ${oneLine(item.verify, 60)}`}
                      </text>
                    </Show>
                    <Show when={item.status === "blocked" && item.evidence}>
                      <text fg={theme.feedbackError}>
                        {`   ↳ ${oneLine(item.evidence ?? "", 80)}`}
                      </text>
                    </Show>
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
              {/* The summary body is the model's own working record, not chat.
                Rendering it inline made ten compactions look like ten
                conversations (measured trace 2026-10-31, t8); the counts and
                the per-layer line say what happened without replaying it. */}
              {row().status === "done" ? (
                <text fg={theme.textMuted}>
                  · {row().beforeMessages}→{row().afterMessages} 条 · {row().inputTokens} in ·{" "}
                  {row().outputTokens} out · 压缩耗 {row().totalTokens}
                </text>
              ) : null}
              {row().status === "done" && row().layers.length ? (
                <text fg={theme.textMuted}>
                  {row()
                    .layers.map(layerLine)
                    .join(" / ")}
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
    <>
      <Index each={props.message.parts}>
        {(part) => <PartView part={part()} alwaysOpen={props.alwaysOpen} />}
      </Index>
      {/* M1b T1b + T7: who answered.  Only a non-default agent earns a tag —
          the build agent is the norm, and a tag on every answer is noise.
          The tag shares agentAccent with composer and status bar. */}
      <Show when={props.message.agent && props.message.agent !== "build"}>
        <box paddingLeft={3}>
          <text fg={agentAccent(props.message.agent ?? "")}>{`· ${props.message.agent}`}</text>
        </box>
      </Show>
    </>
  )
}
