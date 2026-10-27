/**
 * Token display helpers — the Context card and the status bar share these, so
 * they format the same real numbers the same way.
 *
 * `formatTokens` matches OpenCode's `toLocaleString` grouping.
 *
 * `percentUsed` no longer clamps at 100. Measured live: `max_context_tokens`
 * was 48,000 against a model whose real window is 256k, so 237,157 tokens
 * rendered as a flat "100% used" — and a window that reads full while still
 * accepting requests looks like the meter is broken rather than the
 * configuration being wrong. 494% is the true reading and the actionable one.
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
    expect(percentUsed(204800, 256000)).toBe(80)
  })

  test("reports above 100 rather than flattening it", () => {
    expect(percentUsed(237157, 48000)).toBe(494)
  })

  test("guards a zero limit and never goes negative", () => {
    expect(percentUsed(100, 0)).toBe(0)
    expect(percentUsed(-5, 256000)).toBe(0)
  })
})