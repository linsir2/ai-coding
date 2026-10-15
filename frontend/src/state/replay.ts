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
import { MAX_TRANSCRIPT_MESSAGES } from "./session"

/** One persisted message as ``session.load`` returns it (snake-free keys). */
export interface WireMessage {
  role: string
  content: string | null
  timestamp?: string
  toolCallId?: string
  toolName?: string
  toolCalls?: { id: string; name: string; arguments: string }[]
  reasoning?: string
}

const PREVIEW_CHARS = 120

/**
 * The checkpoint note's first line (ai_coding/core/context_compression.py
 * ``CHECKPOINT_MARK``).  Stored as a user-role message so the model reads it
 * as history; rendered as assistant prose so the markdown renders.
 */
const CHECKPOINT_MARK = "[以下为早期对话摘要]"

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

export function messagesToState(messages: WireMessage[]): SessionState {
  const out: ChatMessage[] = []
  for (const message of messages) {
    if (message.role === "user" && !(message.content ?? "").startsWith(CHECKPOINT_MARK)) {
      out.push({
        role: "user",
        parts: [{ kind: "text", text: message.content ?? "", streaming: false }],
      })
      continue
    }
    // Everything after a user message belongs to that turn's assistant
    // message, so replay matches the live stream's one-message-per-turn shape.
    // A checkpoint note lands here too: it is stored user-role but is
    // model-generated markdown, so it replays as assistant prose (markdown
    // renders) rather than a plain user row (it would not).
    let assistant = out[out.length - 1]
    if (!assistant || assistant.role !== "assistant") {
      assistant = { role: "assistant", parts: [] }
      out.push(assistant)
    }
    if (message.role === "user") {
      // Checkpoint note: the only user-role message that reaches here (the
      // plain branch above took all others).  Its body is the summary.
      if (message.content) {
        assistant.parts.push({ kind: "text", text: message.content, streaming: false })
      }
      continue
    }
    if (message.role === "assistant") {      // Reasoning first, then the prose it produced — the same order the live
      // reducer rebuilds, so a reloaded transcript reads like the live one.
      if (message.reasoning) {
        assistant.parts.push({ kind: "reasoning", text: message.reasoning })
      }
      if (message.content) {
        assistant.parts.push({ kind: "text", text: message.content, streaming: false })
      }
      for (const call of message.toolCalls ?? []) {
        assistant.parts.push({
          kind: "tool",
          callId: call.id,
          toolName: call.name,
          verb: toolVerb(call.name),
          target: toolTarget(call.name, parseArguments(call.arguments)),
          status: "running",
          // A replayed card has no stream behind it: nothing to fold, and a
          // pipe tag per line would be a guess.
          lines: [],
          pending: "",
          linesDropped: false,
          preview: "",
          error: "",
          durationMs: 0,
        })
      }
    } else if (message.role === "tool") {
      const callId = message.toolCallId ?? ""
      const content = message.content ?? ""
      const success = !content || !content.split("\n")[0]!.includes('ok="false"')
      const text = shorten(stripEnvelope(content), PREVIEW_CHARS)
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
    }
  }
  // P3-4: a reopened session lands under the same cap as the live
  // transcript (the live side retires in session.ts).  Oldest first.
  const capped =
    out.length > MAX_TRANSCRIPT_MESSAGES ? out.slice(out.length - MAX_TRANSCRIPT_MESSAGES) : out
  return {
    messages: capped.filter((message) => message.role === "user" || message.parts.length > 0),
    busy: false,
    queued: [],
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
 * user's move -- resending the message makes the model ask again by itself.
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
  } & RecoveryFacts
  const state = messagesToState(data.messages ?? [])
  const note = recoveryNotice(data)
  if (note) state.messages.unshift({ role: "assistant", parts: [note] })
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
