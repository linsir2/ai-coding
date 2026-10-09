/**
 * History replay (M3) — persisted messages → the parts model the transcript
 * renders, so a reopened session reads like the turn that produced it.
 *
 * Ports the old Textual replay semantics (ai_coding/ui/blocks.py):
 * ``_strip_envelope`` drops the ``<tool_output>`` wrapper and its machine
 * ``<data>`` block, a first line containing ``ok="false"`` marks failure, and
 * tool cards derive their verb/target from the call arguments.  One turn is
 * one assistant message, exactly like the live event stream.
 */

import { shorten, toolTarget, toolVerb } from "../util/toolmeta"
import type { ChatMessage, Part, SessionState } from "./session"

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
    if (message.role === "user") {
      out.push({
        role: "user",
        parts: [{ kind: "text", text: message.content ?? "", streaming: false }],
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
    if (message.role === "assistant") {
      if (message.content) {
        assistant.parts.push({ kind: "text", text: message.content, streaming: false })
      }
      if (message.reasoning) {
        assistant.parts.push({ kind: "reasoning", text: message.reasoning })
      }
      for (const call of message.toolCalls ?? []) {
        assistant.parts.push({
          kind: "tool",
          callId: call.id,
          toolName: call.name,
          verb: toolVerb(call.name),
          target: toolTarget(call.name, parseArguments(call.arguments)),
          status: "running",
          tail: "",
          preview: "",
          error: "",
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
  return {
    messages: out.filter((message) => message.role === "user" || message.parts.length > 0),
    busy: false,
  }
}
