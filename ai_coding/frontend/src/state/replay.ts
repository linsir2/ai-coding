/**
 * History replay (M3) — persisted messages → the parts model the transcript
 * renders, so a reopened session reads like the turn that produced it.
 *
 * Ports the old Textual replay semantics (ui/blocks.py, now retired):
 * ``_strip_envelope`` drops the ``<tool_output>`` wrapper and its machine
 * ``<data>`` block, a first line containing ``ok="false"`` marks failure, and
 * tool cards derive their verb/target from the call arguments.  One turn is
 * one assistant message, exactly like the live event stream.
 */

import type { IpcClient } from "../ipc"
import { shorten, toolTarget, toolVerb } from "../util/toolmeta"
import type { ChatMessage, Part, SessionState } from "./session"
import { MAX_TRANSCRIPT_MESSAGES, applyPlanUpdate } from "./session"

/** One persisted message as ``session.load`` returns it (snake-free keys). */
export interface WireMessage {
  role: string
  content: string | null
  timestamp?: string
  toolCallId?: string
  toolName?: string
  toolCalls?: { id: string; name: string; arguments: string }[]
  reasoning?: string
  /** D6: the harness wrote it (a plan step marker, a nudge), not the operator. */
  synthetic?: boolean
  /** M1b T1b: the agent that produced an assistant step (absent = older file). */
  agent?: string
}

const PREVIEW_CHARS = 120

/** One durable agent switch, as ``session.load`` returns it (M1b T1b). */
export interface WireAgentSwitch {
  from?: string
  to?: string
  ts?: string
  reason?: string
}

function switchLine(sw: WireAgentSwitch): string {
  const reason = (sw.reason ?? "").trim()
  const suffix = reason ? `（${reason}）` : ""
  return `Switched agent: ${sw.from || "?"} → ${sw.to || "?"}${suffix}`
}

/**
 * The checkpoint note's first line (ai_coding/core/context_compression.py
 * ``CHECKPOINT_MARK``).  Stored as a user-role message so the model reads it
 * as history; rendered as assistant prose so the markdown renders.
 */
const CHECKPOINT_MARK = "[以下为早期对话摘要]"

/** steering 前端化: written by the loop into history (agent_loop::_take_steering). */
const STEERING_MARK = "[steering] "

/**
 * P0-2: a fact the harness learned about the run (agent_loop::_take_notices) --
 * a background sub-agent settling. It is stored user-role because that is how
 * every harness message reaches the model, and it is *not* the operator's
 * words, so it replays as a muted notice instead of a user bubble.
 */
const NOTICE_MARK = "[notice] "

/** Drop the ``<tool_output>`` wrapper and its machine ``<data>`` block. */
export function stripEnvelope(content: string): string {
  const kept: string[] = []
  let inData = false
  const lines = content.split("\n")
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]!
    const stripped = line.trim()
    if (index === 0 && line.startsWith("<tool_output")) continue
    if (stripped === "<data>") {
      inData = true
      continue
    }
    if (stripped === "</data>") {
      inData = false
      continue
    }
    if (inData || stripped === "</tool_output>") continue
    kept.push(line)
  }
  return kept.join("\n").trim()
}

/** Parse a call's JSON argument string; a malformed one yields no params. */
function parseArguments(argumentsText: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(argumentsText)
    return typeof parsed === "object" && parsed !== null
      ? (parsed as Record<string, unknown>)
      : {}
  } catch {
    return {}
  }
}

export function messagesToState(
  messages: WireMessage[],
  switches: WireAgentSwitch[] = [],
): SessionState {
  const out: ChatMessage[] = []

  // M1b T1b: switches are anchored by **timestamp**, not by a message index.
  // ``/compact`` rewrites ``session.messages``, so an index would point at the
  // wrong message afterwards (or past the end); the surviving messages keep
  // their timestamps, so this anchor survives compaction.
  const pending = [...switches].sort((a, b) => (a.ts ?? "").localeCompare(b.ts ?? ""))
  let nextSwitch = 0

  // A switch is committed at a turn boundary, so appending its line to the
  // last assistant bubble keeps the "one assistant message per turn" merge
  // intact — pushing a standalone bubble mid-turn would capture the rest of
  // that turn's prose and tool rows.
  const flushSwitch = (sw: WireAgentSwitch): void => {
    const part: Part = { kind: "notice", tone: "muted", text: switchLine(sw) }
    const last = out[out.length - 1]
    if (last && last.role === "assistant") {
      last.parts.push(part)
    } else {
      out.push({ role: "assistant", parts: [part] })
    }
  }

  const flushBefore = (stamp: string): void => {
    while (nextSwitch < pending.length) {
      const sw = pending[nextSwitch]!
      const when = (sw.ts ?? "").trim()
      if (when && stamp && when >= stamp) break
      flushSwitch(sw)
      nextSwitch++
    }
  }

  for (const message of messages) {
    flushBefore((message.timestamp ?? "").trim())
    if (
      message.role === "user" &&
      (message.content ?? "").startsWith(NOTICE_MARK)
    ) {
      // P0-2: the harness wrote this about the run; it belongs beside the
      // answer, not in the operator's column.
      out.push({
        role: "assistant",
        parts: [
          {
            kind: "notice",
            tone: "muted",
            text: (message.content ?? "").slice(NOTICE_MARK.length),
          },
        ],
      })
      continue
    }
    if (
      message.role === "user" &&
      message.synthetic &&
      !(message.content ?? "").startsWith(STEERING_MARK)
    ) {
      // D6: the stamp is the class -- a plan step's marker, a closing prompt
      // or a decision nudge.  Steering is the one synthetic line that wraps
      // words the operator typed, so it falls through to the user branch.
      continue
    }
    if (
      message.role === "user" &&
      (message.content ?? "").startsWith(CHECKPOINT_MARK)
    ) {
      // The checkpoint is the model's working record, not the conversation.
      // Replaying it as prose re-printed the whole summary after every reload
      // (the same block the live card used to draw); one muted notice says the
      // same thing without replaying it.
      let assistant = out[out.length - 1]
      if (!assistant || assistant.role !== "assistant") {
        assistant = { role: "assistant", parts: [] }
        out.push(assistant)
      }
      assistant.parts.push({ kind: "notice", tone: "muted", text: "早期对话已压缩" })
      continue
    }
    if (message.role === "user" && !(message.content ?? "").startsWith(CHECKPOINT_MARK)) {
      // steering 前端化: "[steering] " is the harness marker the model reads;
      // the operator typed the rest, so a replay shows their own words.
      const raw = message.content ?? ""
      const text = raw.startsWith(STEERING_MARK) ? raw.slice(STEERING_MARK.length) : raw
      out.push({
        role: "user",
        parts: [{ kind: "text", text, streaming: false }],
      })
      continue
    }
    // Everything after a user message belongs to that turn's assistant
    // message, so replay matches the live stream's one-message-per-turn shape.
    let assistant = out[out.length - 1]
    if (!assistant || assistant.role !== "assistant") {
      assistant = { role: "assistant", parts: [] }
      out.push(assistant)
    }
    if (message.role === "assistant") {      // Reasoning first, then the prose it produced — the same order the live
      // reducer rebuilds, so a reloaded transcript reads like the live one.
      if (message.agent) assistant.agent = message.agent
      if (message.reasoning) {
        assistant.parts.push({ kind: "reasoning", text: message.reasoning })
      }
      if (message.content) {
        assistant.parts.push({ kind: "text", text: message.content, streaming: false })
      }
      for (const call of message.toolCalls ?? []) {
        const params = parseArguments(call.arguments)
        assistant.parts.push({
          kind: "tool",
          callId: call.id,
          toolName: call.name,
          verb: toolVerb(call.name),
          target: toolTarget(call.name, params),
          status: "running",
          // A replayed card has no stream behind it: nothing to fold, and a
          // pipe tag per line would be a guess.
          lines: [],
          pending: "",
          linesDropped: false,
          preview: "",
          error: "",
          durationMs: 0,
          approvalMs: 0,
          // M3 T11: only the plan card needs its arguments back; everything
          // else would just duplicate the session file in the transcript.
          ...(call.name === "present_plan" ? { params } : {}),
        })
      }
    } else if (message.role === "tool") {
      const callId = message.toolCallId ?? ""
      const content = message.content ?? ""
      const success = !content || !content.split("\n")[0]!.includes('ok="false"')
      const body = stripEnvelope(content)
      const text = shorten(body, PREVIEW_CHARS)
      for (const part of assistant.parts) {
        if (part.kind !== "tool" || part.callId !== callId) continue
        if (success) {
          part.status = "ok"
          part.preview = text
        } else {
          part.status = "failed"
          part.error = text
        }
      }
      // 23: the research conclusion is the deliverable of that exit, and the
      // persisted tool result holds it whole (the 500/120 truncation is a
      // *display* rule of the tool card, not of the transcript).  Draw it as
      // prose here, exactly as the live path draws the conclusion event.
      if (success && message.toolName === "finish_research" && body) {
        assistant.parts.push({ kind: "text", text: body, streaming: false })
      }
    }
  }
  while (nextSwitch < pending.length) {
    flushSwitch(pending[nextSwitch]!)
    nextSwitch++
  }
  // P3-4: a reopened session lands under the same cap as the live
  // transcript (the live side retires in session.ts).  Oldest first.
  const capped =
    out.length > MAX_TRANSCRIPT_MESSAGES ? out.slice(out.length - MAX_TRANSCRIPT_MESSAGES) : out
  return {
    messages: capped.filter((message) => message.role === "user" || message.parts.length > 0),
    // T7: a replay carries no live posture — the route hydrates
    // SessionState.agent from session.stats right after.
    agent: null,
    // R21 §2: same for the read-only axis; stats fills both on mount.
    readonly: null,
    busy: false,
    queued: [],
    // Replayed history has no live jobs: the panel fills this from
    // session.jobs on mount, and a job from an earlier run arrives as
    // adopted then -- not as a row invented here.
    jobs: [],
    // G2: no live goal in a replay either; the status line is a live
    // indicator fed by plan.updated while a goal runs.
    goal: null,
  }
}

/** The recovery facts ``session.load`` carries beside the transcript (P5). */
export interface RecoveryFacts {
  recoveredInterrupted?: boolean
  openTurn?: number
  pendingApproval?: { toolName?: string; params?: Record<string, unknown> } | null
}

/** What to tell the user about a session that came back from a crash (P5).
 *
 * A pending approval is a *notice*, not a re-issued approval panel: the panel
 * belongs to the turn that died, and with no live turn to receive an answer,
 * asking again would collect a reply that goes nowhere.  Continuing is the
 * user's move -- the notice carries the resume key (08), so one keypress
 * walks the same turn on from the persisted history.
 *
 * 08: the notice now carries the resume action -- the session route binds it
 * to ``ctrl+x r`` (this TUI is keyboard-driven; there is no mouse to click
 * with), so the key hint rides the copy and the renderer stays untouched.
 *
 * Returns ``null`` for an ordinary session, so replay is unchanged for one.
 */
export function recoveryNotice(data: RecoveryFacts): Part | null {
  // 08/addendum-2: the key is only promised when the turn itself survived to
  // disk.  A crash before the first checkpoint leaves the marker but no
  // open turn -- not even the user message reached the file (P2 rides the
  // next save by design) -- so there is nothing to continue from and the
  // backend refuses.  Offering resume there would advertise what the backend
  // then rejects; the notice stays, the action does not.
  const resumable = !!data.recoveredInterrupted
  const keyHint = resumable ? "——按 ctrl+x r 续跑" : ""
  const pending = data.pendingApproval
  if (pending && typeof pending === "object" && pending.toolName) {
    const target = pending.params ? shorten(JSON.stringify(pending.params), 80) : ""
    return {
      kind: "notice",
      tone: "error",
      ...(resumable ? { action: "resume" as const } : {}),
      text: `上一步在等审批：${pending.toolName}${target ? " " + target : ""}（该回合已中断）${keyHint}`,
    }
  }
  if (data.recoveredInterrupted) {
    const turn = data.openTurn ? `（turn ${data.openTurn}）` : ""
    return {
      kind: "notice",
      tone: "error",
      action: "resume",
      text: `上次回合未完成${turn}——按 ctrl+x r 续跑`,
    }
  }
  return null
}

/** Load a persisted session and replay it — shared by the dialog and the tabs. */
export async function loadSession(client: IpcClient, id: string): Promise<SessionState> {
  const data = (await client.request("session.load", { id })) as {
    messages?: WireMessage[]
    // M1b T1b: the durable switch timeline.
    agentHistory?: WireAgentSwitch[]
    // R05: the recovered task rides the load response; the card is repainted
    // through the live reducer so reload and live cannot drift.
    task?: Record<string, unknown> | null
  } & RecoveryFacts
  let state = messagesToState(data.messages ?? [], data.agentHistory ?? [])
  const note = recoveryNotice(data)
  if (note) state.messages.unshift({ role: "assistant", parts: [note] })
  if (data.task && Array.isArray(data.task["items"]) && data.task["items"].length > 0) {
    state = applyPlanUpdate(state, data.task)
  }
  return state
}

/** Ask the backend to continue the interrupted turn (08) — no prompt text
 * rides along, so unlike ``session.prompt`` nothing is echoed.  The turn's
 * wire events stream back the same way a prompted turn's do. */
export async function resumeSession(
  client: IpcClient,
  id: string,
): Promise<{ turnId: number; sessionId: string }> {
  return (await client.request("session.resume", { id })) as {
    turnId: number
    sessionId: string
  }
}
