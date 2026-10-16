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
  /** Whether a run is actually starting (P0-3).  `resumed` cannot say so: the
   *  breaker branch answers it too and launches nothing. */
  resuming?: boolean
}

export function initialRoute(hello: ResumableHello): "home" | "session" {
  return hello.resumed ? "session" : "home"
}

export function resumeNotice(hello: ResumableHello): string {
  return (hello.resumeNote ?? "").trim()
}

export function resumingNow(hello: ResumableHello): boolean {
  return hello.resuming === true
}

/**
 * The landing transform: the operator's note on top, and — when a run is
 * starting — the session already busy.
 *
 * Busy matters because the composer reads it to decide between `session.prompt`
 * and the busy paths (steer / queue).  A resumed run that lands idle takes
 * ordinary prompts, which the loop serialises as *turns* and therefore
 * interleaves between plan steps.
 */
export function withResumeNotice(
  state: SessionState,
  note: string,
  resuming = false,
): SessionState {
  const landed: SessionState = resuming ? { ...state, busy: true } : state
  if (!note) return landed
  return {
    ...landed,
    messages: [
      { role: "assistant", parts: [{ kind: "notice", tone: "muted", text: note }] },
      ...landed.messages,
    ],
  }
}
