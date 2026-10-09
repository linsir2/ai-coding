/**
 * TDD (M4): token display helpers — the Context card and status bar share
 * these.  formatTokens matches OpenCode's `toLocaleString` grouping; the
 * percentage is clamped so a multi-step turn's summed usage can never read
 * over 100%.
 */

import { describe, expect, test } from "bun:test"
import { formatTokens, percentUsed } from "../src/util/stats"

describe("formatTokens", () => {
  test("groups thousands", () => {
    expect(formatTokens(0)).toBe("0")
    expect(formatTokens(13930)).toBe("13,930")
    expect(formatTokens(1234567)).toBe("1,234,567")
  })
})

describe("percentUsed", () => {
  test("rounds the ratio", () => {
    expect(percentUsed(13930, 48000)).toBe(29)
    expect(percentUsed(0, 48000)).toBe(0)
  })

  test("clamps to 0..100 and guards a zero limit", () => {
    expect(percentUsed(60000, 48000)).toBe(100)
    expect(percentUsed(100, 0)).toBe(0)
  })
})
