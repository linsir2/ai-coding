/**
 * TDD (M5): the permission panel's pure half — the option table.
 *
 * Labels are OpenCode's verbatim (`permissionOptionLabel`,
 * util/permission.ts:162); the decisions are the CLI vocabulary the shared
 * backend parser already speaks (security/approval.py:88-98).
 */

import { describe, expect, test } from "bun:test"
import { PERMISSION_OPTIONS } from "../src/util/permission"

describe("permission options", () => {
  test("offers once, always and reject with OpenCode's labels", () => {
    expect(PERMISSION_OPTIONS.map((option) => option.label)).toEqual([
      "Allow once",
      "Always allow",
      "Reject",
    ])
  })

  test("every option decodes to a decision the backend parser accepts", () => {
    const accepted = new Set(["yes", "always", "no", "stop"])
    for (const option of PERMISSION_OPTIONS) {
      expect(accepted.has(option.decision)).toBe(true)
    }
  })

  test("esc's fail-closed answer exists as the reject decision", () => {
    expect(PERMISSION_OPTIONS.find((option) => option.key === "reject")?.decision).toBe("no")
  })
})
