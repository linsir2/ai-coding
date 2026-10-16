import { describe, expect, test } from "bun:test"
import { initialRoute, resumeNotice, withResumeNotice } from "../src/util/boot"
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
