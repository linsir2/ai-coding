/**
 * TDD (A3.1): the permissions dialog's pure half.
 *
 * The dialog only *reads* through ``permissions.state``; every switch it
 * offers is the backend's own command (``/permissions auto|confirm|clear``)
 * so the provider re-arm (I1) keeps a single home.
 */

import { describe, expect, test } from "bun:test"
import {
  policyLabel,
  grantLine,
  toggledPolicy,
  toggleCommand,
} from "../src/util/permissions-dialog"

describe("policyLabel", () => {
  test("names the two policies the backend has (J1)", () => {
    expect(policyLabel("auto")).toBe("Auto-approve")
    expect(policyLabel("confirm")).toBe("Confirm each call")
  })
})

describe("grantLine", () => {
  test("renders a grant key the way the key reads", () => {
    expect(grantLine("write:/tmp/a.py")).toBe("write · /tmp/a.py")
  })

  test("an empty grant list says so instead of rendering nothing", () => {
    expect(grantLine("")).toBe("(none)")
  })
})

describe("the policy toggle (OpenCode's prompt/autoaccept pair)", () => {
  test("toggling moves to the other policy and back", () => {
    expect(toggledPolicy("confirm")).toBe("auto")
    expect(toggledPolicy("auto")).toBe("confirm")
  })

  test("the toggle dispatches the command for the policy it moves to", () => {
    expect(toggleCommand("confirm")).toBe("/permissions auto")
    expect(toggleCommand("auto")).toBe("/permissions confirm")
  })
})
