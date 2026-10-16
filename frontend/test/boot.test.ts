import { describe, expect, test } from "bun:test"
import { initialRoute, resumeNotice, resumingNow, withResumeNotice } from "../src/util/boot"
import { initialState } from "../src/state/session"

describe("boot landing (可达+有痕批次)", () => {
  test("an ordinary hello lands on Home; a resumed one opens the session", () => {
    expect(initialRoute({})).toBe("home")
    expect(initialRoute({ resumed: false })).toBe("home")
    expect(initialRoute({ resumed: true })).toBe("session")
  })

  test("the resume note is trimmed and optional", () => {
    expect(resumeNotice({})).toBe("")
    expect(resumeNotice({ resumeNote: "  [plan] x  " })).toBe("[plan] x")
  })

  test("withResumeNotice unshifts one muted notice; empty is a no-op", () => {
    const state = initialState()
    expect(withResumeNotice(state, "")).toBe(state)

    const noted = withResumeNotice(state, "[plan] 自动续跑")
    expect(noted.messages[0]?.parts).toEqual([
      { kind: "notice", tone: "muted", text: "[plan] 自动续跑" },
    ])
  })
})

describe("boot resume is a running session, not just a landed one (P0-3)", () => {
  test("resumingNow only trusts an explicit true", () => {
    expect(resumingNow({})).toBe(false)
    expect(resumingNow({ resumed: true })).toBe(false)
    expect(resumingNow({ resuming: false })).toBe(false)
    expect(resumingNow({ resuming: true })).toBe(true)
  })

  test("a starting run lands the session busy, so the composer takes the busy path", () => {
    // Otherwise a line typed during the resumed run goes out as a fresh prompt
    // and interleaves between plan steps -- the loop serialises turns, not runs.
    const state = initialState()
    expect(state.busy).toBe(false)

    const running = withResumeNotice(state, "[plan] 续跑", true)
    expect(running.busy).toBe(true)

    // The breaker branch: resumed, noted, and nothing running.
    const paused = withResumeNotice(state, "[plan] 已达上限", false)
    expect(paused.busy).toBe(false)
    expect(paused.messages[0]?.parts).toEqual([
      { kind: "notice", tone: "muted", text: "[plan] 已达上限" },
    ])
  })

  test("a run that starts without a note still lands busy", () => {
    const running = withResumeNotice(initialState(), "", true)
    expect(running.busy).toBe(true)
    expect(running.messages).toHaveLength(0)
  })
})
