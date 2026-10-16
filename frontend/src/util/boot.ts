/**
 * Boot landing (可达+有痕批次): when the backend consumed an armed session at
 * startup, the shell opens THAT session instead of Home -- the resumed run's
 * approvals and transcript live in the session route, and landing on Home
 * made them unreachable (measured in the s5 tmux pass: the resumed turn hung
 * on a write approval nobody could see), and the run was invisible besides.
 */

import type { SessionState } from "../state/session"

export interface ResumableHello {
  resumed?: boolean
  resumeNote?: string
}

export function initialRoute(hello: ResumableHello): "home" | "session" {
  return hello.resumed ? "session" : "home"
}

export function resumeNotice(hello: ResumableHello): string {
  return (hello.resumeNote ?? "").trim()
}

export function withResumeNotice(state: SessionState, note: string): SessionState {
  if (!note) return state
  return {
    ...state,
    messages: [
      { role: "assistant", parts: [{ kind: "notice", tone: "muted", text: note }] },
      ...state.messages,
    ],
  }
}
